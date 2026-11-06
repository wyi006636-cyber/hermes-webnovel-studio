"""Structured Hermes Webnovel runtime actions."""

from __future__ import annotations

import json
import math
import os

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import load_webnovel_config
from .creative_actions import (
    CreativeOverwriteError,
    CreativeProjectService,
)
from .dashboard_service import DASHBOARD_REGISTRY, DashboardError
from .llm_router import RoleLlmRouter
from .novel_writing_service import ChapterAlreadyExistsError, NovelWritingService
from .novel_quality_service import NovelQualityService
from .project_lock import ProjectLock, ProjectLockError
from .run_store import InvalidTransitionError, RunStore, RunStoreError
from .tool_schema import WEBNOVEL_RUNTIME_ACTIONS
from .upstream_bridge import (
    BridgeError,
    BridgeResult,
    UpstreamBridge,
    resolve_project_root,
    resolve_python_executable,
    resolve_upstream_root,
)


_IMPLEMENTED_ACTIONS = frozenset(
    {
        "project_status",
        "project_init",
        "project_plan",
        "review_range",
        "doctor",
        "query",
        "dashboard_start",
        "run_resume",
        "run_abort",
        "write_chapter",
    }
)
_DEPRECATED_ACTIONS = frozenset(
    {"write_prepare", "write_draft", "write_review", "write_revise", "write_commit"}
)
_DIAGNOSTIC_LIMIT = 2000
_DEFAULT_LOCK_TIMEOUT = 10.0
_DEFAULT_DASHBOARD_PORT = 8765
_DASHBOARD_HOST = "127.0.0.1"
class RuntimeProjectLockError(RuntimeError):
    """Adapter-local wrapper that keeps the optional lock dependency lazy."""


class RuntimeInputError(ValueError):
    """Raised when runtime input cannot be safely normalized."""


def _canonical_path(path: Path) -> Path:
    """Return the absolute, symlink-free form of a user-supplied path.

    Symlinked ancestors are normal on real machines (``/tmp`` on macOS, synced
    folders, external drives), so they are resolved rather than refused. Every
    later containment check runs against this canonical path, and links
    *inside* a project are still rejected where the project is inspected.
    """

    requested = path.expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    return requested.resolve(strict=False)


def _prepare_init_target(raw_root: str | os.PathLike[str], payload: Mapping[str, Any]) -> Path:
    if payload.get("confirmed") is not True:
        raise RuntimeInputError("project creation requires confirmed=true")
    target = Path(raw_root).expanduser()
    if not target.is_absolute():
        raise RuntimeInputError("project_init project_root must be an absolute path")
    target = _canonical_path(target)
    adapter_root = Path(__file__).resolve().parent
    upstream_root = resolve_upstream_root().resolve()
    if target == adapter_root or target.is_relative_to(adapter_root):
        raise RuntimeInputError("project_init target must not be inside the adapter")
    if target == upstream_root or target.is_relative_to(upstream_root):
        raise RuntimeInputError("project_init target must not be inside vendored upstream")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeInputError(
            f"project_init parent directory does not exist: {target.parent}"
        ) from exc
    if not parent.is_dir():
        raise RuntimeInputError("project_init target parent is not a directory")
    if target.exists():
        if not target.is_dir():
            raise RuntimeInputError("project_init target must be a safe directory")
        state_path = target / ".webnovel" / "state.json"
        if state_path.is_symlink():
            raise RuntimeInputError("project_init state marker must not be a symlink")
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeInputError("project_init state marker is invalid") from exc
            if (
                not isinstance(state, dict)
                or state.get("schema_version") != "novel-init-pending/v1"
                or state.get("project_info")
                or state.get("project")
            ):
                raise RuntimeInputError("project_init requires a fresh project target")
        elif any(target.iterdir()):
            raise RuntimeInputError("uninitialized project_init target must be empty")
    return target


def _resolve_human_novel_root(raw_root: str | os.PathLike[str]) -> Path:
    requested = _canonical_path(Path(raw_root))
    try:
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise RuntimeInputError(f"novel project does not exist: {requested}") from exc
    if not root.is_dir():
        raise RuntimeInputError("novel project root must be a directory")
    metadata = root / ".webnovel"
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_dir()):
        raise RuntimeInputError("novel project metadata path is unsafe")
    story_files: list[Path] = []
    for directory_name in ("设定集", "大纲", "正文"):
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for candidate in directory.rglob("*.md"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                continue
            story_files.append(candidate)
            break
        if story_files:
            break
    if not story_files:
        raise RuntimeInputError(
            "directory is not a novel project: no safe Markdown found in 设定集, 大纲, or 正文"
        )
    return root


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _base_envelope(args: Any) -> dict[str, Any]:
    if isinstance(args, Mapping):
        action = args.get("action")
        raw_root = args.get("project_root")
        raw_run_id = args.get("run_id")
    else:
        action = None
        raw_root = None
        raw_run_id = None
    project_root = os.fspath(raw_root) if isinstance(raw_root, (str, os.PathLike)) else None
    run_id = raw_run_id if isinstance(raw_run_id, str) else None
    return {
        "success": False,
        "action": action if isinstance(action, str) else None,
        "project_root": project_root,
        "run_id": run_id,
        "status": "error",
        "outputs": {},
        "errors": [],
    }


def _fail(
    envelope: dict[str, Any], code: str, message: str, **details: Any
) -> dict[str, Any]:
    envelope["success"] = False
    if code == "deprecated_action":
        envelope["status"] = "deprecated"
    elif code.startswith("not_implemented"):
        envelope["status"] = "not_implemented"
    else:
        envelope["status"] = "error"
    envelope["errors"] = [_error(code, message, **details)]
    return envelope


def _payload(args: Mapping[str, Any]) -> dict[str, Any]:
    value = args.get("payload", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeInputError("payload must be a JSON object")
    return value


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    extras = sorted(str(key) for key in set(payload).difference(allowed))
    if extras:
        raise RuntimeInputError(f"{label} payload contains unsupported fields: {extras}")


def _positive_int(value: Any, *, label: str, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeInputError(f"{label} must be a positive integer")
    return value


def _optional_chapter(args: Mapping[str, Any]) -> int | None:
    return _positive_int(args.get("chapter"), label="chapter")


def _strict_json(stdout: str) -> Any:
    if not stdout.strip():
        raise ValueError("upstream stdout was empty")

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-standard JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        stdout,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )


def _diagnostic(result: BridgeResult) -> dict[str, str]:
    def truncate(value: str) -> str:
        if len(value) <= _DIAGNOSTIC_LIMIT:
            return value
        return value[:_DIAGNOSTIC_LIMIT] + "…"

    return {
        "stdout": truncate(result.stdout),
        "stderr": truncate(result.stderr),
    }


def _consume_upstream(
    envelope: dict[str, Any],
    result: BridgeResult,
    *,
    success_status: str = "ok",
    extra_outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "returncode": result.returncode,
        "command": list(result.command),
    }
    if extra_outputs:
        outputs.update(extra_outputs)
    try:
        parsed = _strict_json(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        envelope["outputs"] = outputs
        return _fail(
            envelope,
            "upstream_invalid_json",
            f"upstream stdout was not strict JSON: {exc}",
            returncode=result.returncode,
            diagnostic=_diagnostic(result),
        )

    outputs["result"] = parsed
    envelope["outputs"] = outputs
    if result.returncode != 0:
        return _fail(
            envelope,
            "upstream_nonzero",
            f"upstream command exited with return code {result.returncode}",
            returncode=result.returncode,
            diagnostic=_diagnostic(result),
        )

    envelope["success"] = True
    envelope["status"] = success_status
    envelope["errors"] = []
    return envelope


def _query_chapter(
    args: Mapping[str, Any], payload: Mapping[str, Any], *, allow_at_chapter: bool
) -> int:
    candidates: list[tuple[str, Any]] = []
    if "chapter" in args:
        candidates.append(("chapter", args.get("chapter")))
    if "chapter" in payload:
        candidates.append(("payload.chapter", payload.get("chapter")))
    if allow_at_chapter and "at_chapter" in payload:
        candidates.append(("payload.at_chapter", payload.get("at_chapter")))
    if not candidates:
        raise RuntimeInputError("chapter is required for this query")
    values = [
        _positive_int(value, label=label, required=True) for label, value in candidates
    ]
    chapter = values[0]
    if any(value != chapter for value in values[1:]):
        raise RuntimeInputError("conflicting chapter values were provided")
    assert chapter is not None
    return chapter


def _query_argv(
    args: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    query_type = payload.get("query_type")
    if not isinstance(query_type, str) or not query_type:
        raise RuntimeInputError("payload.query_type is required")

    if query_type in {"entity_state", "relationships"}:
        _reject_extra(
            payload,
            {"query_type", "entity", "chapter", "at_chapter"},
            label="query",
        )
        entity = payload.get("entity")
        if not isinstance(entity, str) or not entity:
            raise RuntimeInputError("payload.entity must be a non-empty string")
        chapter = _query_chapter(args, payload, allow_at_chapter=True)
        command = (
            "query-entity-state" if query_type == "entity_state" else "query-relationships"
        )
        return (
            "knowledge",
            (command, "--entity", entity, "--at-chapter", str(chapter)),
        )

    if query_type == "rules":
        _reject_extra(payload, {"query_type", "domain"}, label="query")
        if args.get("chapter") is not None:
            raise RuntimeInputError(
                "rules query accepts payload.domain, not a chapter"
            )
        domain = payload.get("domain", "")
        if not isinstance(domain, str):
            raise RuntimeInputError("payload.domain must be a string")
        return "memory-contract", ("query-rules", "--domain", domain)

    if query_type == "open_loops":
        _reject_extra(payload, {"query_type"}, label="query")
        return "memory-contract", ("get-open-loops",)

    if query_type == "context":
        _reject_extra(payload, {"query_type", "chapter"}, label="query")
        chapter = _query_chapter(args, payload, allow_at_chapter=False)
        return "memory-contract", ("load-context", "--chapter", str(chapter))

    if query_type == "project_memory":
        _reject_extra(
            payload,
            {
                "query_type",
                "operation",
                "pattern_type",
                "description",
                "category",
                "importance",
                "source_chapter",
            },
            label="query",
        )
        if payload.get("operation") != "add_pattern":
            raise RuntimeInputError(
                "project_memory operation must be 'add_pattern'"
            )
        pattern_type = payload.get("pattern_type")
        description = payload.get("description")
        category = payload.get("category", "")
        importance = payload.get("importance", "medium")
        if pattern_type not in {
            "hook",
            "pacing",
            "dialogue",
            "payoff",
            "emotion",
            "format",
            "other",
        }:
            raise RuntimeInputError("project memory pattern_type is invalid")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeInputError("project memory description is required")
        if not isinstance(category, str):
            raise RuntimeInputError("project memory category must be a string")
        if importance not in {"high", "medium", "low"}:
            raise RuntimeInputError("project memory importance is invalid")
        arguments = [
            "add-pattern",
            "--pattern-type",
            pattern_type,
            "--description",
            description.strip(),
            "--category",
            category,
            "--importance",
            importance,
        ]
        source_chapter = payload.get("source_chapter")
        if source_chapter is not None:
            chapter = _positive_int(
                source_chapter,
                label="payload.source_chapter",
                required=True,
            )
            arguments.extend(["--source-chapter", str(chapter)])
        return "project-memory", tuple(arguments)

    raise RuntimeInputError(f"unknown query_type: {query_type!r}")


def _lock_timeout(payload: Mapping[str, Any]) -> float:
    value = payload.get("lock_timeout", _DEFAULT_LOCK_TIMEOUT)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise RuntimeInputError("payload.lock_timeout must be finite and nonnegative")
    return float(value)


def _require_run_id(envelope: Mapping[str, Any]) -> str:
    run_id = envelope.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeInputError("run_id is required for this action")
    return run_id


def _dashboard(
    args: Mapping[str, Any], envelope: dict[str, Any], project_root: Path
) -> dict[str, Any]:
    payload = _payload(args)
    _reject_extra(payload, {"operation", "port"}, label="dashboard")
    operation = payload.get("operation", "start")
    if operation not in {"start", "inspect", "plan", "stop"}:
        raise RuntimeInputError(
            "payload.operation must be 'start', 'inspect', 'plan', or 'stop'"
        )
    requested_port = payload.get("port")
    if requested_port is not None and (
        not isinstance(requested_port, int)
        or isinstance(requested_port, bool)
        or requested_port < 1
        or requested_port > 65535
    ):
        raise RuntimeInputError("payload.port must be an integer from 1 to 65535")

    if operation == "stop":
        stopped = DASHBOARD_REGISTRY.stop(project_root)
        envelope["success"] = True
        envelope["status"] = "stopped" if stopped else "not_running"
        envelope["outputs"] = {"stopped": stopped}
        return envelope

    running = DASHBOARD_REGISTRY.inspect(project_root)
    if operation == "inspect" and running is not None:
        envelope["success"] = True
        envelope["status"] = "running"
        envelope["outputs"] = {
            "ready": True,
            "launched": True,
            "host": running.host,
            "port": running.port,
            "url": running.url,
            "health": running.health,
            "preflight": running.preflight,
            "no_browser": True,
        }
        return envelope

    upstream_root = resolve_upstream_root()
    python = resolve_python_executable()
    server = upstream_root / "dashboard" / "server.py"
    frontend_index = upstream_root / "dashboard" / "frontend" / "dist" / "index.html"
    ready = server.is_file() and frontend_index.is_file()
    port = requested_port or _DEFAULT_DASHBOARD_PORT
    command = [
        str(python),
        "-m",
        "dashboard.server",
        "--project-root",
        str(project_root),
        "--host",
        _DASHBOARD_HOST,
        "--port",
        str(port),
        "--no-browser",
    ]
    common = {
        "ready": ready,
        "launched": False,
        "paths": {
            "upstream_root": str(upstream_root),
            "server": str(server),
            "frontend_index": str(frontend_index),
            "cwd": str(upstream_root),
        },
        "command": command,
        "host": _DASHBOARD_HOST,
        "port": port,
        "no_browser": True,
        "url": f"http://{_DASHBOARD_HOST}:{port}",
    }
    if not ready:
        envelope["outputs"] = common
        return _fail(
            envelope,
            "dashboard_not_ready",
            "dashboard launch assets are missing",
            missing=[
                str(path)
                for path in (server, frontend_index)
                if not path.is_file()
            ],
        )
    if operation in {"inspect", "plan"}:
        envelope["success"] = True
        envelope["status"] = "ready"
        envelope["outputs"] = common
        return envelope

    try:
        handle = DASHBOARD_REGISTRY.start(project_root, port=requested_port)
    except DashboardError as exc:
        return _fail(envelope, "dashboard_start_failed", str(exc))
    actual_command = list(common["command"])
    actual_command[actual_command.index("--port") + 1] = str(handle.port)
    envelope["success"] = True
    envelope["status"] = "running"
    envelope["outputs"] = {
        **common,
        "ready": True,
        "launched": True,
        "command": actual_command,
        "host": handle.host,
        "port": handle.port,
        "url": handle.url,
        "health": handle.health,
        "preflight": handle.preflight,
    }
    return envelope


def _execute_resolved(
    args: Mapping[str, Any],
    envelope: dict[str, Any],
    project_root: Path,
    *,
    llm: Any = None,
) -> dict[str, Any]:
    action = envelope["action"]
    assert isinstance(action, str)

    if action not in WEBNOVEL_RUNTIME_ACTIONS:
        raise RuntimeInputError(f"unknown action: {action!r}")
    if action not in _IMPLEMENTED_ACTIONS:
        return _fail(
            envelope,
            "not_implemented",
            f"action {action!r} is reserved for a later implementation task",
        )

    if action == "write_chapter":
        if llm is None:
            return _fail(
                envelope,
                "dependency_missing",
                "write_chapter requires the bound Hermes plugin LLM",
            )
        payload = _payload(args)
        _reject_extra(
            payload,
            {"instructions", "user_input", "target_chars", "overwrite"},
            label=action,
        )
        raw_instruction = payload.get("instructions", payload.get("user_input", ""))
        if isinstance(raw_instruction, str):
            instruction = raw_instruction
        elif isinstance(raw_instruction, Mapping):
            instruction = json.dumps(raw_instruction, ensure_ascii=False)
        elif isinstance(raw_instruction, (list, tuple)):
            instruction = "\n".join(str(item) for item in raw_instruction)
        elif raw_instruction is None:
            instruction = ""
        else:
            instruction = str(raw_instruction)

        raw_target = payload.get("target_chars")
        target_warning = False
        if isinstance(raw_target, str) and raw_target.strip().isdigit():
            raw_target = int(raw_target.strip())
        if raw_target is not None and (
            isinstance(raw_target, bool)
            or not isinstance(raw_target, int)
            or raw_target < 1
        ):
            raw_target = None
            target_warning = True

        raw_overwrite = payload.get("overwrite", False)
        if isinstance(raw_overwrite, str):
            normalized = raw_overwrite.strip().casefold()
            if normalized in {"true", "yes", "1"}:
                raw_overwrite = True
            elif normalized in {"false", "no", "0", ""}:
                raw_overwrite = False
        if not isinstance(raw_overwrite, bool):
            raise RuntimeInputError("payload.overwrite must be a boolean")

        router = RoleLlmRouter(llm, load_webnovel_config())
        chapter = _positive_int(args.get("chapter"), label="chapter", required=True)
        assert chapter is not None
        try:
            result = NovelWritingService(project_root, router=router).write_chapter(
                chapter,
                author_instruction=instruction,
                target_chars=raw_target,
                overwrite=raw_overwrite,
            )
        except ChapterAlreadyExistsError as exc:
            return _fail(envelope, "chapter_exists", str(exc))
        envelope["success"] = True
        outputs = dict(result)
        if target_warning:
            warnings = list(outputs.get("warnings") or [])
            warnings.append("target_chars_ignored")
            outputs["warnings"] = list(dict.fromkeys(warnings))
            if outputs.get("status") == "written":
                outputs["status"] = "written_with_warnings"
        envelope["status"] = str(outputs["status"])
        envelope["outputs"] = outputs
        return envelope


    if action == "project_init":
        if llm is None:
            return _fail(envelope, "dependency_missing", "project_init requires the bound Hermes plugin LLM")
        payload = _payload(args)
        router = RoleLlmRouter(llm, load_webnovel_config())
        service = CreativeProjectService(project_root, router=router)
        if payload.get("confirmed") is not True:
            idea = payload.get("idea", payload.get("instructions", payload))
            proposal = service.propose(idea)
            envelope["success"] = True
            envelope["status"] = "needs_confirmation"
            envelope["outputs"] = {
                "proposal": proposal,
                "project_root": str(project_root),
                "confirmation_prompt": "确认这份小说方案后再创建项目。",
            }
            return envelope
        raw_proposal = payload.get("proposal")
        proposal = raw_proposal if isinstance(raw_proposal, Mapping) else {
            key: value
            for key, value in payload.items()
            if key not in {"confirmed", "lock_timeout"}
        }
        try:
            result = service.initialize(proposal)
        except CreativeOverwriteError as exc:
            return _fail(envelope, "project_exists", str(exc))
        envelope["success"] = True
        envelope["status"] = str(result["status"])
        envelope["outputs"] = dict(result)
        return envelope

    if action == "project_plan":
        if llm is None:
            return _fail(envelope, "dependency_missing", "project_plan requires the bound Hermes plugin LLM")
        payload = _payload(args)
        volume = _positive_int(args.get("volume"), label="volume", required=True)
        assert volume is not None

        def optional_chapter(name: str) -> int | None:
            value = payload.get(name)
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RuntimeInputError(f"payload.{name} must be a positive integer")
            return value

        overwrite = payload.get("confirmed_overwrite", payload.get("overwrite", False))
        if isinstance(overwrite, str):
            overwrite = overwrite.strip().casefold() in {"true", "yes", "1"}
        if not isinstance(overwrite, bool):
            raise RuntimeInputError("payload.confirmed_overwrite must be a boolean")
        requirements = payload.get("user_requirements", payload.get("instructions", ""))
        service = CreativeProjectService(
            project_root,
            router=RoleLlmRouter(llm, load_webnovel_config()),
        )
        try:
            result = service.plan_volume(
                volume,
                start_chapter=optional_chapter("start_chapter"),
                end_chapter=optional_chapter("end_chapter"),
                requirements=requirements,
                overwrite=overwrite,
            )
        except CreativeOverwriteError as exc:
            return _fail(envelope, "plan_exists", str(exc))
        envelope["success"] = True
        envelope["status"] = str(result["status"])
        envelope["outputs"] = dict(result)
        return envelope

    if action == "review_range":
        if llm is None:
            return _fail(envelope, "dependency_missing", "review_range requires the bound Hermes plugin LLM")
        payload = _payload(args)

        def review_chapter(name: str, fallback: Any = None) -> int:
            value = payload.get(name, fallback)
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RuntimeInputError(f"payload.{name} must be a positive integer")
            return value

        start = review_chapter("start_chapter", args.get("chapter"))
        end = review_chapter("end_chapter", start)
        result = NovelQualityService(
            project_root,
            router=RoleLlmRouter(llm, load_webnovel_config()),
        ).review_range(start, end)
        envelope["success"] = True
        envelope["status"] = str(result["status"])
        envelope["outputs"] = dict(result)
        return envelope

    if action == "project_status":
        chapter = _optional_chapter(args)
        if envelope["run_id"] is not None:
            record = RunStore(project_root).load(_require_run_id(envelope))
            envelope["success"] = True
            envelope["status"] = record["status"]
            envelope["outputs"] = {"ledger": record}
            return envelope
        bridge = UpstreamBridge(project_root=project_root)
        arguments: list[str] = []
        if chapter is not None:
            arguments.extend(["--chapter", str(chapter)])
        arguments.extend(["--format", "json"])
        result = bridge.run("project-status", *arguments)
        consumed = _consume_upstream(envelope, result)
        parsed = consumed["outputs"].get("result")
        if consumed["success"] and isinstance(parsed, dict):
            phase = parsed.get("phase")
            if isinstance(phase, str) and phase:
                consumed["status"] = phase
        return consumed

    if action == "doctor":
        payload = _payload(args)
        _reject_extra(payload, {"deep"}, label="doctor")
        deep = payload.get("deep", False)
        if not isinstance(deep, bool):
            raise RuntimeInputError("payload.deep must be a boolean")
        chapter = _optional_chapter(args)
        bridge = UpstreamBridge(project_root=project_root)
        result = bridge.doctor(output_format="json", chapter=chapter, deep=deep)
        return _consume_upstream(envelope, result)

    if action == "query":
        payload = _payload(args)
        subcommand, query_arguments = _query_argv(args, payload)
        bridge = UpstreamBridge(project_root=project_root)
        result = bridge.run(subcommand, *query_arguments)
        return _consume_upstream(
            envelope,
            result,
            extra_outputs={"query_type": payload["query_type"]},
        )

    if action == "dashboard_start":
        return _dashboard(args, envelope, project_root)

    payload = _payload(args)
    allowed = {"lock_timeout"} if action == "run_resume" else {"lock_timeout", "reason"}
    _reject_extra(payload, allowed, label=action)
    run_id = _require_run_id(envelope)
    timeout = _lock_timeout(payload)
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise RuntimeInputError("payload.reason must be a string")

    try:
        with ProjectLock(project_root, timeout=timeout):
            store = RunStore(project_root)
            if action == "run_resume":
                record = store.resume(run_id)
            else:
                record = store.abort(run_id, reason=reason)
    except ProjectLockError as exc:
        raise RuntimeProjectLockError(str(exc)) from exc
    envelope["success"] = True
    envelope["status"] = record["status"]
    envelope["outputs"] = {"ledger": record}
    return envelope


def execute_runtime(
    args: dict[str, Any] | Mapping[str, Any] | Any, *, llm: Any = None
) -> dict[str, Any]:
    """Execute one runtime action and always return a structured JSON-compatible object."""

    envelope = _base_envelope(args)
    try:
        if not isinstance(args, Mapping):
            raise RuntimeInputError("tool arguments must be a JSON object")
        action = args.get("action")
        if not isinstance(action, str) or not action:
            raise RuntimeInputError("action must be a non-empty string")
        if action in _DEPRECATED_ACTIONS:
            return _fail(
                envelope,
                "deprecated_action",
                f"action {action!r} is deprecated; use 'write_chapter' instead",
                replacement="write_chapter",
            )
        raw_root = args.get("project_root")
        if not isinstance(raw_root, (str, os.PathLike)):
            raise RuntimeInputError("project_root is required and must be a path string")
        if action == "project_init":
            payload = _payload(args)
            if payload.get("confirmed") is True:
                project_root = _prepare_init_target(raw_root, payload)
            else:
                target = Path(raw_root).expanduser()
                if not target.is_absolute():
                    raise RuntimeInputError("project_init project_root must be an absolute path")
                project_root = _canonical_path(target)
                if not project_root.parent.is_dir():
                    raise RuntimeInputError(
                        f"project_init parent directory does not exist: {project_root.parent}"
                    )
        elif action in {"write_chapter", "review_range"}:
            project_root = _resolve_human_novel_root(raw_root)
        else:
            project_root = resolve_project_root(raw_root, environ={})
        envelope["project_root"] = str(project_root)
        return _execute_resolved(args, envelope, project_root, llm=llm)
    except RuntimeInputError as exc:
        return _fail(envelope, "invalid_arguments", str(exc))
    except InvalidTransitionError as exc:
        return _fail(envelope, "invalid_transition", str(exc))
    except RuntimeProjectLockError as exc:
        return _fail(envelope, "project_lock_error", str(exc))
    except ProjectLockError as exc:
        return _fail(envelope, "project_lock_error", str(exc))
    except RunStoreError as exc:
        return _fail(envelope, "run_store_error", str(exc))
    except subprocess.TimeoutExpired as exc:
        return _fail(
            envelope,
            "upstream_timeout",
            f"upstream command timed out after {exc.timeout} seconds",
            timeout=exc.timeout,
        )
    except BridgeError as exc:
        return _fail(envelope, "bridge_error", str(exc))
    except Exception as exc:
        return _fail(
            envelope,
            "runtime_error",
            f"runtime action failed safely: {type(exc).__name__}: {exc}",
        )


def runtime_handler(
    args: dict[str, Any] | Any = None, *, llm: Any = None, **kwargs: Any
) -> str:
    """Hermes handler boundary: return strict JSON and never leak a traceback."""

    # Hermes invokes plugin tools with keyword arguments, while direct tests and
    # embedders may pass one positional mapping plus execution-context kwargs.
    call_args: Any = kwargs if args is None else args
    try:
        result = execute_runtime(call_args, llm=llm)
        return json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except Exception as exc:
        fallback = {
            "success": False,
            "action": call_args.get("action") if isinstance(call_args, Mapping) else None,
            "project_root": (
                call_args.get("project_root") if isinstance(call_args, Mapping) else None
            ),
            "run_id": call_args.get("run_id") if isinstance(call_args, Mapping) else None,
            "status": "error",
            "outputs": {},
            "errors": [
                _error(
                    "serialization_error",
                    f"runtime response could not be serialized safely: {type(exc).__name__}",
                )
            ],
        }
        return json.dumps(
            fallback,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            default=lambda value: f"<{type(value).__name__}>",
        )


__all__ = ["execute_runtime", "runtime_handler"]

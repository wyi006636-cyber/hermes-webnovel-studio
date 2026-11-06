"""Real-model acceptance runner for the novel-first Hermes plugin."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .runtime import execute_runtime


class LiveAcceptanceError(RuntimeError):
    """Raised when the installed novel workflow fails its real-model proof."""


def _require_success(step: str, result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("success") is not True:
        raise LiveAcceptanceError(
            f"{step} failed: {json.dumps(dict(result), ensure_ascii=False, sort_keys=True)}"
        )
    return dict(result)


def _outputs(step: str, result: Mapping[str, Any]) -> dict[str, Any]:
    value = result.get("outputs")
    if not isinstance(value, Mapping):
        raise LiveAcceptanceError(f"{step} returned no outputs")
    return dict(value)


def _strict_object(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveAcceptanceError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise LiveAcceptanceError(f"expected JSON object: {path}")
    return value


def _artifact_path(project_root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise LiveAcceptanceError(f"{label} path is missing")
    path = Path(value).expanduser().resolve(strict=True)
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise LiveAcceptanceError(f"{label} escaped the project root") from exc
    if not path.is_file() or path.is_symlink():
        raise LiveAcceptanceError(f"{label} is not a safe file")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_written_chapter(
    project_root: Path, result: Mapping[str, Any], *, chapter: int
) -> tuple[dict[str, Any], Path]:
    outputs = _outputs(f"write_chapter {chapter}", result)
    chapter_path = _artifact_path(
        project_root,
        outputs.get("chapter_file"),
        label=f"chapter {chapter} manuscript",
    )
    manuscript = chapter_path.read_text(encoding="utf-8")
    parts = manuscript.split("\n\n", 1)
    body = parts[1].strip() if len(parts) == 2 else ""
    if not body:
        raise LiveAcceptanceError(f"write_chapter {chapter} produced no prose")
    if outputs.get("character_count") != len(body):
        raise LiveAcceptanceError(
            f"write_chapter {chapter} reported an incorrect character count"
        )
    forbidden_terms = ("source_id", "CBN", "CPN", "CEN", "draft_sha256")
    leaked = [term for term in forbidden_terms if term in manuscript]
    if leaked:
        raise LiveAcceptanceError(f"chapter {chapter} leaked internal terms: {leaked}")
    calls = outputs.get("model_calls")
    if not isinstance(calls, list):
        raise LiveAcceptanceError(f"write_chapter {chapter} returned no model-call audit")
    stages = [call.get("stage") for call in calls if isinstance(call, Mapping)]
    if stages != ["draft", "edit", "memory"]:
        raise LiveAcceptanceError(
            f"write_chapter {chapter} did not use the exact draft/edit/memory sequence"
        )
    if chapter > 1:
        draft_call = calls[0] if isinstance(calls[0], Mapping) else {}
        continuity = draft_call.get("continuity")
        required_continuity = (
            "previous_ending",
            "recent_memory",
            "memory_character_changes",
            "memory_open_threads",
            "memory_ending",
        )
        if not isinstance(continuity, Mapping) or any(
            continuity.get(key) is not True for key in required_continuity
        ):
            raise LiveAcceptanceError(
                f"write_chapter {chapter} did not receive complete prior-story continuity"
            )
    if any(call.get("failed") is True for call in calls if isinstance(call, Mapping)):
        raise LiveAcceptanceError(f"write_chapter {chapter} required a failed-stage retry")
    return outputs, chapter_path


def run_live_acceptance(project_root: Path, *, llm: Any) -> dict[str, Any]:
    """Exercise init, plan, write, and optional literary review on a fresh project."""

    project_root = project_root.expanduser().resolve(strict=False)
    if project_root.exists():
        raise LiveAcceptanceError(
            f"live acceptance requires a fresh absent project path: {project_root}"
        )
    project_root.parent.mkdir(parents=True, exist_ok=True)

    proposal_result = _require_success(
        "project_init proposal",
        execute_runtime(
            {
                "action": "project_init",
                "project_root": str(project_root),
                "payload": {
                    "confirmed": False,
                    "idea": (
                        "现实悬疑长篇：记者林舟收到失踪三年的码头工当天寄出的湿钥匙，"
                        "必须进入封闭旧塔查清真相。人物驱动，克制，对白有潜台词。"
                    ),
                },
            },
            llm=llm,
        ),
    )
    if proposal_result.get("status") != "needs_confirmation":
        raise LiveAcceptanceError("project_init proposal did not request one confirmation")
    if project_root.exists():
        raise LiveAcceptanceError("project_init proposal wrote files before confirmation")
    proposal = _outputs("project_init proposal", proposal_result).get("proposal")
    if not isinstance(proposal, Mapping) or not str(proposal.get("title") or "").strip():
        raise LiveAcceptanceError("project_init proposal is incomplete")

    init_result = _require_success(
        "project_init confirmed",
        execute_runtime(
            {
                "action": "project_init",
                "project_root": str(project_root),
                "payload": {"confirmed": True, "proposal": dict(proposal)},
            },
            llm=llm,
        ),
    )
    init_outputs = _outputs("project_init confirmed", init_result)

    plan_result = _require_success(
        "project_plan",
        execute_runtime(
            {
                "action": "project_plan",
                "project_root": str(project_root),
                "volume": 1,
                "payload": {
                    "start_chapter": 1,
                    "end_chapter": 2,
                    "user_requirements": (
                        "林舟单视角。第一章验证钥匙来源并进入旧塔；"
                        "章末塔内电话响起，来电显示是林舟自己的号码。"
                    ),
                },
            },
            llm=llm,
        ),
    )
    plan_outputs = _outputs("project_plan", plan_result)
    outline_path = _artifact_path(
        project_root, plan_outputs.get("outline_file"), label="volume outline"
    )
    outline = outline_path.read_text(encoding="utf-8")
    if "### 第1章" not in outline or "### 第2章" not in outline:
        raise LiveAcceptanceError("volume outline does not contain the requested chapters")

    write_results: dict[int, dict[str, Any]] = {}
    write_outputs: dict[int, dict[str, Any]] = {}
    chapter_paths: dict[int, Path] = {}
    write_requests = {
        1: {
            "instructions": "克制现实，人物行动推进，对白有潜台词，结尾保留明确悬念。",
            "target_chars": 3500,
        },
        2: {
            "instructions": "紧接上一章结尾，不复述第一章；让人物对异常来电做出具体选择。",
            "target_chars": "随故事需要",
        },
    }
    for chapter, payload in write_requests.items():
        result = _require_success(
            f"write_chapter {chapter}",
            execute_runtime(
                {
                    "action": "write_chapter",
                    "project_root": str(project_root),
                    "chapter": chapter,
                    "payload": payload,
                },
                llm=llm,
            ),
        )
        outputs, chapter_path = _validate_written_chapter(
            project_root, result, chapter=chapter
        )
        write_results[chapter] = result
        write_outputs[chapter] = outputs
        chapter_paths[chapter] = chapter_path
    if "target_chars_ignored" not in list(write_outputs[2].get("warnings") or []):
        raise LiveAcceptanceError("invalid optional target_chars was not downgraded to a warning")

    memory_path = project_root / ".webnovel" / "novel_memory.json"
    memory = _strict_object(memory_path)
    chapters = memory.get("chapters")
    if not isinstance(chapters, Mapping) or any(
        not isinstance(chapters.get(str(chapter)), Mapping) for chapter in (1, 2)
    ):
        raise LiveAcceptanceError("lightweight story memory does not contain both chapters")

    manuscript_before_review = {
        chapter: _sha256(path) for chapter, path in chapter_paths.items()
    }
    review_result = _require_success(
        "review_range",
        execute_runtime(
            {
                "action": "review_range",
                "project_root": str(project_root),
                "payload": {"start_chapter": 1, "end_chapter": 2},
            },
            llm=llm,
        ),
    )
    review_outputs = _outputs("review_range", review_result)
    reports = review_outputs.get("reports")
    if not isinstance(reports, list) or len(reports) != 2:
        raise LiveAcceptanceError("review_range did not create exactly two quality reports")
    report_paths = [
        _artifact_path(project_root, value, label=f"quality report {index}")
        for index, value in enumerate(reports, start=1)
    ]
    manuscript_after_review = {
        chapter: _sha256(path) for chapter, path in chapter_paths.items()
    }
    if manuscript_after_review != manuscript_before_review:
        raise LiveAcceptanceError("optional literary review modified the manuscript")
    if review_outputs.get("manuscripts_modified") is not False:
        raise LiveAcceptanceError("optional literary review did not prove read-only behavior")

    required_files = [
        project_root / ".webnovel" / "state.json",
        project_root / ".webnovel" / "creative_proposal.json",
        project_root / "设定集" / "核心设定.md",
        project_root / "大纲" / "总纲.md",
    ]
    if not all(path.is_file() and not path.is_symlink() for path in required_files):
        raise LiveAcceptanceError("initialized project is missing human-readable source files")
    if (project_root / ".story-system").exists():
        raise LiveAcceptanceError("novel-first acceptance unexpectedly created Story System")

    return {
        "success": True,
        "schema": "webnovel-novel-first-live/v2",
        "project_root": str(project_root),
        "title": init_outputs.get("title"),
        "chapters": [
            {
                "chapter": chapter,
                "file": str(chapter_paths[chapter]),
                "title": write_outputs[chapter].get("title"),
                "character_count": write_outputs[chapter].get("character_count"),
                "status": write_results[chapter].get("status"),
            }
            for chapter in (1, 2)
        ],
        "model_call_count": sum(
            len(list(write_outputs[chapter].get("model_calls") or []))
            for chapter in (1, 2)
        ),
        "memory_file": str(memory_path),
        "quality_reports": [str(path) for path in report_paths],
        "manuscript_sha256_before_review": {
            str(chapter): value for chapter, value in manuscript_before_review.items()
        },
        "manuscript_sha256_after_review": {
            str(chapter): value for chapter, value in manuscript_after_review.items()
        },
        "story_system_created": False,
        "steps": {
            "proposal": proposal_result.get("status"),
            "init": init_result.get("status"),
            "plan": plan_result.get("status"),
            "write": {
                str(chapter): write_results[chapter].get("status")
                for chapter in (1, 2)
            },
            "review": review_result.get("status"),
        },
    }


__all__ = ["LiveAcceptanceError", "run_live_acceptance"]
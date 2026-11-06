from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import webnovel_studio
from webnovel_studio.project_lock import ProjectLock, ProjectLockError
from webnovel_studio.run_store import RunStore
from webnovel_studio.tool_schema import (
    WEBNOVEL_RUNTIME_ACTIONS,
    WEBNOVEL_RUNTIME_SCHEMA,
)
from webnovel_studio.upstream_bridge import BridgeResult


IMPLEMENTED_ACTIONS = {
    "project_status",
    "project_init",
    "project_plan",
    "doctor",
    "query",
    "dashboard_start",
    "run_resume",
    "run_abort",
    "write_chapter",
    "review_range",
}
NOT_IMPLEMENTED_ACTIONS = set(WEBNOVEL_RUNTIME_ACTIONS) - IMPLEMENTED_ACTIONS


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    metadata = path / ".webnovel"
    metadata.mkdir()
    (metadata / "state.json").write_text(
        json.dumps({"project": {"title": "雨夜长歌"}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    outline = path / "大纲"
    outline.mkdir()
    (outline / "总纲.md").write_text("# 雨夜长歌总纲\n", encoding="utf-8")
    return path.resolve()


def create_failed_run(project: Path, run_id: str = "run-failed") -> RunStore:
    store = RunStore(project)
    store.create(run_id, chapter=1, volume=1, mode="write", request={"语言": "中文"})
    store.fail(run_id, "PREPARED", "暂时失败")
    return store


def assert_envelope(result: dict[str, Any], action: str, project: Path) -> None:
    assert result["success"] is True or result["success"] is False
    assert result["action"] == action
    assert result["project_root"] == str(project)
    assert result["run_id"] is None or isinstance(result["run_id"], str)
    assert isinstance(result["status"], str)
    assert isinstance(result["outputs"], dict)
    assert isinstance(result["errors"], list)
    for error in result["errors"]:
        assert isinstance(error["code"], str)
        assert isinstance(error["message"], str)


class RecordingBridge:
    calls: list[tuple[str, tuple[str, ...]]] = []
    result = BridgeResult(
        command=("python", "webnovel.py", "fixture"),
        returncode=0,
        stdout='{"fixture": true}',
        stderr="",
    )

    def __init__(self, *, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, subcommand: str, *arguments: str, timeout: float | None = None) -> BridgeResult:
        del timeout
        type(self).calls.append((subcommand, arguments))
        return type(self).result

    def doctor(
        self,
        *,
        output_format: str = "json",
        chapter: int | None = None,
        deep: bool = False,
        timeout: float | None = None,
    ) -> BridgeResult:
        arguments: list[str] = ["--format", output_format]
        if chapter is not None:
            arguments.extend(["--chapter", str(chapter)])
        if deep:
            arguments.append("--deep")
        return self.run("doctor", *arguments, timeout=timeout)


@pytest.fixture(autouse=True)
def reset_recording_bridge() -> None:
    RecordingBridge.calls = []
    RecordingBridge.result = BridgeResult(
        command=("python", "webnovel.py", "fixture"),
        returncode=0,
        stdout='{"fixture": true}',
        stderr="",
    )


def test_schema_has_one_source_of_truth_and_exposes_one_chapter_write_action() -> None:
    from webnovel_studio import runtime

    expected = [
        "project_status",
        "project_init",
        "project_plan",
        "write_chapter",
        "review_range",
        "query",
        "doctor",
        "dashboard_start",
        "run_resume",
        "run_abort",
    ]
    assert WEBNOVEL_RUNTIME_ACTIONS == expected
    assert runtime._IMPLEMENTED_ACTIONS == frozenset(expected)
    assert NOT_IMPLEMENTED_ACTIONS == set()
    for legacy in ("write_prepare", "write_draft", "write_review", "write_revise", "write_commit"):
        assert legacy not in WEBNOVEL_RUNTIME_ACTIONS
    assert WEBNOVEL_RUNTIME_SCHEMA["parameters"]["properties"]["action"]["enum"] is WEBNOVEL_RUNTIME_ACTIONS
    assert webnovel_studio.WEBNOVEL_RUNTIME_SCHEMA is WEBNOVEL_RUNTIME_SCHEMA

    class Context:
        def register_tool(self, **kwargs: Any) -> None:
            self.tool = kwargs

        def register_hook(self, *args: Any) -> None:
            del args

        def register_skill(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def register_cli_command(self, **kwargs: Any) -> None:
            del kwargs

    context = Context()
    webnovel_studio.register(context)
    assert context.tool["schema"] is WEBNOVEL_RUNTIME_SCHEMA


def test_write_chapter_requires_bound_hermes_llm(tmp_path: Path) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / "write-chapter")
    result = execute_runtime(
        {"action": "write_chapter", "project_root": str(project), "chapter": 1}
    )

    assert_envelope(result, "write_chapter", project)
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "dependency_missing"


def test_write_chapter_normalizes_natural_payload_and_returns_manuscript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "novel-write")
    seen: dict[str, Any] = {}

    class Service:
        def __init__(self, project_root: Path, *, router: Any) -> None:
            seen.update(project_root=project_root, router=router)

        def write_chapter(self, chapter: int, **kwargs: Any) -> dict[str, Any]:
            seen.update(chapter=chapter, kwargs=kwargs)
            return {
                "status": "written",
                "chapter": chapter,
                "title": "雨夜",
                "chapter_file": str(project / "正文" / "第0003章-雨夜.md"),
                "character_count": 1234,
                "warnings": [],
                "model_calls": [],
                "next_command": "/webnovel-write 4",
            }

    router = object()
    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: router)
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())
    monkeypatch.setattr(runtime, "NovelWritingService", Service)
    result = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 3,
            "payload": {
                "instructions": ["对白克制", "结尾留悬念"],
                "target_chars": "3000",
                "overwrite": "false",
            },
        },
        llm=object(),
    )

    assert result["success"] is True
    assert result["status"] == "written"
    assert seen["chapter"] == 3
    assert seen["kwargs"] == {
        "author_instruction": "对白克制\n结尾留悬念",
        "target_chars": 3000,
        "overwrite": False,
    }


def test_invalid_target_chars_is_ignored_with_warning_instead_of_blocking_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "soft-length")
    seen: dict[str, Any] = {}

    class Service:
        def __init__(self, project_root: Path, *, router: Any) -> None:
            del project_root, router

        def write_chapter(self, chapter: int, **kwargs: Any) -> dict[str, Any]:
            seen.update(chapter=chapter, kwargs=kwargs)
            return {
                "status": "written",
                "chapter": chapter,
                "title": "雨夜",
                "chapter_file": str(project / "正文" / "第0001章-雨夜.md"),
                "character_count": 100,
                "warnings": [],
                "model_calls": [],
                "next_command": "/webnovel-write 2",
            }

    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: object())
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())
    monkeypatch.setattr(runtime, "NovelWritingService", Service)

    result = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
            "payload": {"target_chars": "随故事需要"},
        },
        llm=object(),
    )

    assert result["success"] is True
    assert result["status"] == "written_with_warnings"
    assert seen["kwargs"]["target_chars"] is None
    assert result["outputs"]["warnings"] == ["target_chars_ignored"]


def test_runtime_confirmed_overwrite_replaces_and_backs_up_legacy_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "overwrite")
    (project / "大纲").mkdir(exist_ok=True)
    (project / "设定集").mkdir()
    (project / "正文").mkdir()
    (project / "大纲" / "第1卷-详细大纲.md").write_text(
        "### 第1章 新潮\n\n旧港潮水倒流。\n", encoding="utf-8"
    )
    (project / "设定集" / "核心设定.md").write_text(
        "旧港每天午夜退潮。\n", encoding="utf-8"
    )
    legacy = project / "正文" / "第1章-作者旧稿.md"
    original = b"# chapter 1\n\noriginal author prose\n"
    legacy.write_bytes(original)

    class Router:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete(self, role: str, messages: Any, *, purpose: str) -> Any:
            del role, messages
            self.calls.append(purpose)
            return SimpleNamespace(
                text="潮声退去后，石阶上留下了一串逆行脚印。",
                provider="test",
                model="test",
                usage={},
                audit={},
            )

        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            del role
            self.calls.append(str(kwargs.get("purpose")))
            return SimpleNamespace(
                parsed={
                    "summary": "旧港潮退，石阶出现逆行脚印。",
                    "character_changes": ["主角决定追踪脚印"],
                    "open_threads": ["脚印来自哪里"],
                    "ending": "脚印消失在封死的仓门前。",
                }
            )

    router = Router()
    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: router)
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())

    refused = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
            "payload": {"overwrite": False},
        },
        llm=object(),
    )
    assert refused["success"] is False
    assert refused["errors"][0]["code"] == "chapter_exists"
    assert legacy.read_bytes() == original
    assert router.calls == []

    written = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
            "payload": {"overwrite": True},
        },
        llm=object(),
    )
    assert written["success"] is True
    current = list((project / "正文").glob("第*章*.md"))
    assert len(current) == 1
    assert current[0].name.startswith("第0001章-")
    assert b"original author prose" not in current[0].read_bytes()
    backups = list((project / ".webnovel" / "backups" / "chapters").glob("*.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert router.calls == [
        "webnovel:novel:draft",
        "webnovel:novel:edit",
        "webnovel:novel:memory",
    ]


def test_runtime_writes_from_human_story_files_without_state_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = tmp_path / "human-files-only"
    (project / "大纲").mkdir(parents=True)
    (project / "设定集").mkdir()
    (project / "大纲" / "第1卷-详细大纲.md").write_text(
        "### 第1章 无编号的门\n\n林舟推开旧塔地下一扇没有编号的门。\n",
        encoding="utf-8",
    )
    (project / "设定集" / "核心设定.md").write_text(
        "旧塔没有地下室，这是作者确认的事实。\n", encoding="utf-8"
    )

    class Router:
        def complete(self, role: str, messages: Any, *, purpose: str) -> Any:
            del role, messages
            return SimpleNamespace(
                text="门后没有楼梯，只有潮湿的风从砖缝里吹出来。",
                provider="test",
                model="test",
                usage={},
                audit={},
            )

        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            del role, kwargs
            return SimpleNamespace(
                parsed={
                    "summary": "林舟发现门后是封死的砖墙。",
                    "character_changes": ["林舟决定检查砖缝"],
                    "open_threads": ["砖缝后的风从何而来"],
                    "ending": "砖墙另一侧传来电话铃声。",
                }
            )

    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: Router())
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())

    result = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
        },
        llm=object(),
    )

    assert result["success"] is True
    assert "state_missing" in result["outputs"]["warnings"]
    assert Path(result["outputs"]["chapter_file"]).is_file()
    assert not (project / ".webnovel" / "state.json").exists()


@pytest.mark.parametrize("action", ["project_init", "project_plan", "write_chapter", "review_range"])
def test_novel_core_lock_failures_use_project_lock_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / action) if action != "project_init" else tmp_path / action

    class CreativeService:
        def __init__(self, project_root: Path, *, router: Any) -> None:
            del project_root, router

        def initialize(self, proposal: Any) -> Any:
            del proposal
            raise ProjectLockError("busy")

        def plan_volume(self, volume: int, **kwargs: Any) -> Any:
            del volume, kwargs
            raise ProjectLockError("busy")

    class WritingService:
        def __init__(self, project_root: Path, *, router: Any) -> None:
            del project_root, router

        def write_chapter(self, chapter: int, **kwargs: Any) -> Any:
            del chapter, kwargs
            raise ProjectLockError("busy")

    class QualityService:
        def __init__(self, project_root: Path, *, router: Any) -> None:
            del project_root, router

        def review_range(self, start: int, end: int) -> Any:
            del start, end
            raise ProjectLockError("busy")

    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: object())
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())
    monkeypatch.setattr(runtime, "CreativeProjectService", CreativeService)
    monkeypatch.setattr(runtime, "NovelWritingService", WritingService)
    monkeypatch.setattr(runtime, "NovelQualityService", QualityService)
    request: dict[str, Any] = {
        "action": action,
        "project_root": str(project),
        "payload": {},
    }
    if action == "project_init":
        request["payload"] = {"confirmed": True, "title": "新书"}
    elif action == "project_plan":
        request["volume"] = 1
    elif action == "write_chapter":
        request["chapter"] = 1
    else:
        request["payload"] = {"start_chapter": 1, "end_chapter": 1}

    result = runtime.execute_runtime(request, llm=object())

    assert result["success"] is False
    assert result["errors"][0]["code"] == "project_lock_error"


def test_project_status_with_run_id_loads_ledger_without_building_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "中文项目")
    store = RunStore(project)
    store.create("run-status", chapter=3, volume=1, mode="write", request={})

    def forbidden_bridge(**kwargs: Any) -> None:
        raise AssertionError(f"upstream bridge must not be created: {kwargs}")

    monkeypatch.setattr(runtime, "UpstreamBridge", forbidden_bridge)
    result = runtime.execute_runtime(
        {"action": "project_status", "project_root": str(project), "run_id": "run-status"}
    )

    assert_envelope(result, "project_status", project)
    assert result["success"] is True
    assert result["status"] == "NEW"
    assert result["outputs"]["ledger"]["run_id"] == "run-status"


def test_real_project_status_and_doctor_are_read_only_on_minimal_chinese_project(
    tmp_path: Path,
) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / "真实中文小说")
    state_before = (project / ".webnovel" / "state.json").read_bytes()

    status = execute_runtime(
        {"action": "project_status", "project_root": str(project), "chapter": 1}
    )
    doctor = execute_runtime(
        {
            "action": "doctor",
            "project_root": str(project),
            "chapter": 1,
            "payload": {"deep": False},
        }
    )

    assert_envelope(status, "project_status", project)
    assert status["success"] is True
    assert status["outputs"]["result"]["project_root"] == str(project)
    assert_envelope(doctor, "doctor", project)
    assert doctor["success"] is (doctor["outputs"]["returncode"] == 0)
    assert doctor["outputs"]["returncode"] in {0, 1}
    assert isinstance(doctor["outputs"]["result"]["ok"], bool)
    assert (project / ".webnovel" / "state.json").read_bytes() == state_before
    assert not (project / ".webnovel" / "runs").exists()


@pytest.mark.parametrize(
    ("query_type", "extra", "expected"),
    [
        (
            "entity_state",
            {"entity": "hero; touch /tmp/never", "chapter": 7},
            (
                "knowledge",
                "query-entity-state",
                "--entity",
                "hero; touch /tmp/never",
                "--at-chapter",
                "7",
            ),
        ),
        (
            "relationships",
            {"entity": "反派 --deep", "at_chapter": 8},
            (
                "knowledge",
                "query-relationships",
                "--entity",
                "反派 --deep",
                "--at-chapter",
                "8",
            ),
        ),
        (
            "rules",
            {"domain": "力量体系"},
            ("memory-contract", "query-rules", "--domain", "力量体系"),
        ),
        ("open_loops", {}, ("memory-contract", "get-open-loops")),
        ("context", {"chapter": 10}, ("memory-contract", "load-context", "--chapter", "10")),
        (
            "project_memory",
            {
                "operation": "add_pattern",
                "pattern_type": "hook",
                "description": "开场先给失败代价",
                "category": "开篇",
                "importance": "high",
            },
            (
                "project-memory",
                "add-pattern",
                "--pattern-type",
                "hook",
                "--description",
                "开场先给失败代价",
                "--category",
                "开篇",
                "--importance",
                "high",
            ),
        ),
    ],
)
def test_query_whitelist_builds_only_fixed_argv_and_preserves_entity_as_one_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_type: str,
    extra: dict[str, Any],
    expected: tuple[str, ...],
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / query_type)
    monkeypatch.setattr(runtime, "UpstreamBridge", RecordingBridge)
    payload = {"query_type": query_type, **extra}

    result = runtime.execute_runtime(
        {"action": "query", "project_root": str(project), "payload": payload}
    )

    assert_envelope(result, "query", project)
    assert result["success"] is True
    assert RecordingBridge.calls == [(expected[0], expected[1:])]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query_type": "unknown"},
        {"query_type": "entity_state", "chapter": 1},
        {"query_type": "relationships", "entity": "hero"},
        {"query_type": "rules", "domain": 7},
        {"query_type": "context", "chapter": True},
        {"query_type": "open_loops", "argv": ["doctor", "--deep"]},
    ],
)
def test_query_missing_unknown_or_extra_command_inputs_fail_closed_before_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "query-invalid")
    monkeypatch.setattr(runtime, "UpstreamBridge", RecordingBridge)
    result = runtime.execute_runtime(
        {"action": "query", "project_root": str(project), "payload": payload}
    )

    assert result["success"] is False
    assert result["errors"][0]["code"] == "invalid_arguments"
    assert RecordingBridge.calls == []


def test_doctor_requires_boolean_deep_and_forwards_top_level_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "doctor")
    monkeypatch.setattr(runtime, "UpstreamBridge", RecordingBridge)

    good = runtime.execute_runtime(
        {
            "action": "doctor",
            "project_root": str(project),
            "chapter": 11,
            "payload": {"deep": True},
        }
    )
    bad = runtime.execute_runtime(
        {
            "action": "doctor",
            "project_root": str(project),
            "payload": {"deep": "yes"},
        }
    )

    assert good["success"] is True
    assert RecordingBridge.calls == [
        ("doctor", ("--format", "json", "--chapter", "11", "--deep"))
    ]
    assert bad["success"] is False
    assert bad["errors"][0]["code"] == "invalid_arguments"


def test_dashboard_returns_readiness_and_safe_launch_plan_without_starting_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "dashboard")

    def forbidden_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"dashboard process must not start: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    result = runtime.execute_runtime(
        {
            "action": "dashboard_start",
            "project_root": str(project),
            "payload": {"operation": "plan", "port": 9876},
        }
    )

    assert_envelope(result, "dashboard_start", project)
    assert result["success"] is True
    outputs = result["outputs"]
    assert outputs["ready"] is True
    assert outputs["launched"] is False
    assert outputs["host"] == "127.0.0.1"
    assert outputs["port"] == 9876
    assert outputs["no_browser"] is True
    assert outputs["command"][1:3] == ["-m", "dashboard.server"]
    assert outputs["command"][-1] == "--no-browser"
    assert Path(outputs["paths"]["server"]).is_file()
    assert Path(outputs["paths"]["frontend_index"]).is_file()


def test_dashboard_start_operation_uses_verified_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "dashboard-start")

    class Registry:
        stopped = False

        def inspect(self, project_root: Path) -> None:
            assert project_root == project
            return None

        def start(self, project_root: Path, *, port: int | None = None) -> Any:
            assert project_root == project
            assert port is None
            return SimpleNamespace(
                host="127.0.0.1",
                port=9911,
                url="http://127.0.0.1:9911",
                health={"status": "ok"},
                preflight={"ok": True},
            )

        def stop(self, project_root: Path) -> bool:
            assert project_root == project
            self.stopped = True
            return True

    registry = Registry()
    monkeypatch.setattr(runtime, "DASHBOARD_REGISTRY", registry)
    started = runtime.execute_runtime(
        {
            "action": "dashboard_start",
            "project_root": str(project),
            "payload": {"operation": "start"},
        }
    )
    stopped = runtime.execute_runtime(
        {
            "action": "dashboard_start",
            "project_root": str(project),
            "payload": {"operation": "stop"},
        }
    )

    assert started["success"] is True
    assert started["status"] == "running"
    assert started["outputs"]["launched"] is True
    assert started["outputs"]["url"] == "http://127.0.0.1:9911"
    assert stopped["success"] is True
    assert stopped["status"] == "stopped"
    assert registry.stopped is True


@pytest.mark.parametrize("port", [0, 65536, True, "8765"])
def test_dashboard_rejects_invalid_port(tmp_path: Path, port: Any) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / f"port-{port}")
    result = execute_runtime(
        {
            "action": "dashboard_start",
            "project_root": str(project),
            "payload": {"operation": "inspect", "port": port},
        }
    )
    assert result["success"] is False
    assert result["errors"][0]["code"] == "invalid_arguments"


def test_resume_and_abort_mutate_real_ledgers_under_the_single_project_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / "ledger")
    store = create_failed_run(project)
    store.create("run-abort", chapter=2, volume=1, mode="write", request={})

    def forbidden_bridge(**kwargs: Any) -> None:
        raise AssertionError(f"upstream bridge must not be created: {kwargs}")

    monkeypatch.setattr(runtime, "UpstreamBridge", forbidden_bridge)
    resumed = runtime.execute_runtime(
        {
            "action": "run_resume",
            "project_root": str(project),
            "run_id": "run-failed",
            "payload": {"lock_timeout": 0.5},
        }
    )
    aborted = runtime.execute_runtime(
        {
            "action": "run_abort",
            "project_root": str(project),
            "run_id": "run-abort",
            "payload": {"lock_timeout": 0.5, "reason": "用户取消"},
        }
    )

    assert_envelope(resumed, "run_resume", project)
    assert resumed["success"] is True
    assert resumed["status"] == "FAILED_PREPARED"
    assert resumed["outputs"]["ledger"]["resume_count"] == 1
    assert_envelope(aborted, "run_abort", project)
    assert aborted["success"] is True
    assert aborted["status"] == "ABORTED"
    assert aborted["outputs"]["ledger"]["abort"]["reason"] == "用户取消"
    assert (project / ".webnovel" / "run.lock").is_file()


@pytest.mark.parametrize("lock_timeout", [-1, math.inf, math.nan, True, "1"])
def test_mutations_require_finite_nonnegative_lock_timeout(
    tmp_path: Path, lock_timeout: Any
) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / "lock-timeout")
    create_failed_run(project)
    result = execute_runtime(
        {
            "action": "run_resume",
            "project_root": str(project),
            "run_id": "run-failed",
            "payload": {"lock_timeout": lock_timeout},
        }
    )

    assert result["success"] is False
    assert result["errors"][0]["code"] == "invalid_arguments"


def test_runtime_reports_real_lock_contention_as_structured_error(tmp_path: Path) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / "locked")
    create_failed_run(project)

    with ProjectLock(project, timeout=0.1):
        result = execute_runtime(
            {
                "action": "run_resume",
                "project_root": str(project),
                "run_id": "run-failed",
                "payload": {"lock_timeout": 0},
            }
        )

    assert result["success"] is False
    assert result["errors"][0]["code"] == "project_lock_error"


@pytest.mark.parametrize(
    ("action", "run_id", "setup", "code"),
    [
        ("project_status", "missing", "none", "run_store_error"),
        ("run_resume", None, "none", "invalid_arguments"),
        ("run_abort", None, "none", "invalid_arguments"),
        ("run_resume", "run-new", "new", "invalid_transition"),
        ("run_resume", "run-aborted", "aborted", "invalid_transition"),
        ("run_abort", "run-aborted", "aborted", "invalid_transition"),
    ],
)
def test_missing_run_nonfailed_resume_and_terminal_mutations_fail_structured(
    tmp_path: Path,
    action: str,
    run_id: str | None,
    setup: str,
    code: str,
) -> None:
    from webnovel_studio.runtime import execute_runtime

    project = make_project(tmp_path / f"{action}-{setup}")
    store = RunStore(project)
    if setup in {"new", "aborted"}:
        store.create(run_id or "unused", chapter=1, volume=1, mode="write", request={})
    if setup == "aborted":
        store.abort(run_id or "unused")

    args: dict[str, Any] = {"action": action, "project_root": str(project)}
    if run_id is not None:
        args["run_id"] = run_id
    result = execute_runtime(args)

    assert result["success"] is False
    assert result["errors"][0]["code"] == code


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_code"),
    [
        (7, '{"partial": true}', "upstream_nonzero"),
        (0, "", "upstream_invalid_json"),
        (0, "not json", "upstream_invalid_json"),
        (0, "NaN", "upstream_invalid_json"),
    ],
)
def test_upstream_nonzero_empty_or_invalid_json_never_looks_successful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    expected_code: str,
) -> None:
    from webnovel_studio import runtime

    project = make_project(tmp_path / f"bad-upstream-{returncode}")
    RecordingBridge.result = BridgeResult(
        command=("python", "webnovel.py", "project-status"),
        returncode=returncode,
        stdout=stdout,
        stderr="诊断" * 2000,
    )
    monkeypatch.setattr(runtime, "UpstreamBridge", RecordingBridge)

    result = runtime.execute_runtime(
        {"action": "project_status", "project_root": str(project)}
    )

    assert result["success"] is False
    assert result["errors"][0]["code"] == expected_code
    assert result["errors"][0]["returncode"] == returncode
    assert len(result["errors"][0]["diagnostic"]["stderr"]) <= 2001


def test_project_outside_a_real_novel_and_bad_top_level_input_are_json_errors(
    tmp_path: Path,
) -> None:
    from webnovel_studio.runtime import runtime_handler

    outside = tmp_path / "outside"
    outside.mkdir()
    for args in (
        {"action": "doctor", "project_root": str(outside)},
        {"action": "doctor"},
        ["not", "an", "object"],
    ):
        raw = runtime_handler(args)  # type: ignore[arg-type]
        result = json.loads(raw)
        assert isinstance(raw, str)
        assert result["success"] is False
        assert isinstance(result["errors"], list)


def test_handler_always_returns_strict_json_and_accepts_hermes_kwargs(
    tmp_path: Path,
) -> None:
    from webnovel_studio.runtime import runtime_handler

    project = make_project(tmp_path / "handler")
    raw = runtime_handler(
        {"action": "project_init", "project_root": str(project)},
        task_id="task-1",
        session_id="session-1",
    )
    parsed = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))

    assert isinstance(raw, str)
    assert parsed["success"] is False
    assert parsed["errors"][0]["code"] == "dependency_missing"

    keyword_raw = runtime_handler(
        action="project_init",
        project_root=str(project),
    )
    keyword_parsed = json.loads(keyword_raw)
    assert keyword_parsed["action"] == "project_init"
    assert keyword_parsed["project_root"] == str(project)
    assert keyword_parsed["errors"][0]["code"] == "dependency_missing"


@pytest.mark.parametrize(
    "action",
    ["write_prepare", "write_draft", "write_review", "write_revise", "write_commit"],
)
def test_legacy_write_actions_return_explicit_deprecated_response(action: str) -> None:
    from webnovel_studio import runtime

    result = runtime.execute_runtime({"action": action})

    assert result["success"] is False
    assert result["status"] == "deprecated"
    assert result["errors"][0]["code"] == "deprecated_action"
    assert result["errors"][0]["replacement"] == "write_chapter"


def test_write_and_review_reject_existing_directory_without_novel_markdown(
    tmp_path: Path,
) -> None:
    from webnovel_studio import runtime

    root = tmp_path / "empty-directory"
    root.mkdir()

    requests = (
        {"action": "write_chapter", "project_root": str(root), "chapter": 1},
        {
            "action": "review_range",
            "project_root": str(root),
            "payload": {"start_chapter": 1, "end_chapter": 1},
        },
    )
    for request in requests:
        result = runtime.execute_runtime(request, llm=object())
        assert result["success"] is False
        assert result["errors"][0]["code"] == "invalid_arguments"
        assert "not a novel project" in result["errors"][0]["message"]

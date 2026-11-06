from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from webnovel_studio import runtime


class FlowRouter:
    def __init__(self) -> None:
        self.draft_contexts: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []

    def complete(self, role: str, messages: list[dict[str, str]], *, purpose: str) -> Any:
        del role
        self.calls.append(purpose)
        payload = json.loads(messages[-1]["content"])
        context = payload["story_context"]
        chapter = int(context["chapter"])
        if purpose == "webnovel:novel:draft":
            self.draft_contexts[chapter] = context
            text = (
                "林舟把耳朵贴上砖墙。电话在砖墙后响起。"
                if chapter == 1
                else "铃声停下时，林舟在砖缝里摸到一根仍在震动的电话线。"
            )
            return SimpleNamespace(
                text=text,
                provider="test",
                model="test",
                usage={},
                audit={},
            )
        if purpose == "webnovel:novel:edit":
            if chapter == 1:
                raise TimeoutError("editor unavailable")
            return SimpleNamespace(
                text="铃声停下时，林舟从砖缝里抽出一根仍在震动的电话线。",
                provider="test",
                model="test",
                usage={},
                audit={},
            )
        raise AssertionError(purpose)

    def complete_structured(self, role: str, **kwargs: Any) -> Any:
        del role
        purpose = str(kwargs.get("purpose") or "")
        self.calls.append(purpose)
        if purpose == "webnovel:creative:plan":
            return SimpleNamespace(
                parsed={
                    "volume_title": "旧塔来电",
                    "volume_arc": "林舟从怀疑钥匙来源转向主动追查旧塔电话。",
                    "chapter_cards": [
                        {
                            "chapter": 1,
                            "title": "砖墙后的铃声",
                            "event": "林舟进入旧塔并确认铃声来自封死的砖墙后。",
                            "character_choice": "林舟选择独自留下检查砖墙。",
                            "ending_drive": "墙后电话显示来电者是林舟自己。",
                        },
                        {
                            "chapter": 2,
                            "title": "震动的电话线",
                            "event": "林舟沿砖缝抽出一根没有接入任何线路的电话线。",
                            "character_choice": "林舟选择接通电话线另一端。",
                            "ending_drive": "听筒里有人准确说出他下一步的动作。",
                        },
                    ],
                }
            )
        if purpose == "webnovel:novel:memory":
            raw = json.loads(kwargs["input"][0]["text"])
            chapter = int(raw["chapter"])
            if chapter == 1:
                raise TimeoutError("memory unavailable")
            return SimpleNamespace(
                parsed={
                    "chapter": chapter,
                    "title": raw["title"],
                    "summary": "林舟从砖缝中抽出异常电话线。",
                    "character_changes": ["林舟决定接通电话"],
                    "open_threads": ["电话线另一端是谁"],
                    "ending": "对方说出林舟下一步动作。",
                }
            )
        raise AssertionError(purpose)


class SuccessfulMemoryRouter(FlowRouter):
    def complete(self, role: str, messages: list[dict[str, str]], *, purpose: str) -> Any:
        if purpose == "webnovel:novel:edit":
            payload = json.loads(messages[-1]["content"])
            chapter = int(payload["story_context"]["chapter"])
            if chapter == 1:
                self.calls.append(purpose)
                return SimpleNamespace(text="第一章编辑稿。电话在砖墙后响起。")
        return super().complete(role, messages, purpose=purpose)

    def complete_structured(self, role: str, **kwargs: Any) -> Any:
        if kwargs.get("purpose") == "webnovel:novel:memory":
            self.calls.append("webnovel:novel:memory")
            raw = json.loads(kwargs["input"][0]["text"])
            chapter = int(raw["chapter"])
            return SimpleNamespace(
                parsed={
                    "chapter": chapter,
                    "title": raw["title"],
                    "summary": f"SUMMARY-SENTINEL-{chapter}",
                    "character_changes": [f"CHARACTER-SENTINEL-{chapter}"],
                    "open_threads": [f"THREAD-SENTINEL-{chapter}"],
                    "ending": f"ENDING-SENTINEL-{chapter}",
                }
            )
        return super().complete_structured(role, **kwargs)


def _initialize_and_plan(
    project: Path, router: FlowRouter, monkeypatch: Any
) -> object:
    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: router)
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())
    llm = object()
    initialized = runtime.execute_runtime(
        {
            "action": "project_init",
            "project_root": str(project),
            "payload": {
                "confirmed": True,
                "proposal": {
                    "title": "旧塔来电",
                    "genre": "现实悬疑",
                    "premise": "记者收到失踪者当天寄出的旧塔钥匙。",
                    "protagonist": "林舟，敏锐但不轻信证人。",
                    "core_conflict": "林舟必须判断异常来电是在示警还是诱导。",
                    "world": "潮湿衰败的旧港。",
                    "tone": "克制现实",
                    "opening": "一把仍在滴水的旧塔钥匙送到报社。",
                    "long_arc": "林舟追查旧港失踪案，并学会承担信任的代价。",
                },
            },
        },
        llm=llm,
    )
    assert initialized["success"] is True
    planned = runtime.execute_runtime(
        {
            "action": "project_plan",
            "project_root": str(project),
            "volume": 1,
            "payload": {"start_chapter": 1, "end_chapter": 2},
        },
        llm=llm,
    )
    assert planned["success"] is True
    return llm


def test_production_composed_flow_continues_from_prose_after_optional_failures(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "novel"
    router = FlowRouter()
    monkeypatch.setattr(runtime, "RoleLlmRouter", lambda llm, config: router)
    monkeypatch.setattr(runtime, "load_webnovel_config", lambda: object())
    llm = object()

    initialized = runtime.execute_runtime(
        {
            "action": "project_init",
            "project_root": str(project),
            "payload": {
                "confirmed": True,
                "proposal": {
                    "title": "旧塔来电",
                    "genre": "现实悬疑",
                    "premise": "记者收到失踪者当天寄出的旧塔钥匙。",
                    "protagonist": "林舟，敏锐但不轻信证人。",
                    "core_conflict": "林舟必须判断异常来电是在示警还是诱导。",
                    "world": "潮湿衰败的旧港。",
                    "tone": "克制现实",
                    "opening": "一把仍在滴水的旧塔钥匙送到报社。",
                    "long_arc": "林舟追查旧港失踪案，并学会承担信任的代价。",
                },
            },
        },
        llm=llm,
    )
    assert initialized["success"] is True

    planned = runtime.execute_runtime(
        {
            "action": "project_plan",
            "project_root": str(project),
            "volume": 1,
            "payload": {"start_chapter": 1, "end_chapter": 2},
        },
        llm=llm,
    )
    assert planned["success"] is True

    first = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
        },
        llm=llm,
    )
    assert first["success"] is True
    assert first["status"] == "written_with_warnings"
    assert "editor_failed" in first["outputs"]["warnings"]
    assert "memory_failed" in first["outputs"]["warnings"]
    assert [call["stage"] for call in first["outputs"]["model_calls"]] == [
        "draft",
        "edit",
        "memory",
    ]
    first_path = Path(first["outputs"]["chapter_file"])
    assert first_path.is_file()
    assert "电话在砖墙后响起" in first_path.read_text(encoding="utf-8")
    memory_path = project / ".webnovel" / "novel_memory.json"
    assert not memory_path.exists()

    second = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 2,
        },
        llm=llm,
    )
    assert second["success"] is True
    second_path = Path(second["outputs"]["chapter_file"])
    assert second_path.is_file()
    assert "电话线" in second_path.read_text(encoding="utf-8")
    assert "电话在砖墙后响起" in router.draft_contexts[2]["previous_ending"]
    assert router.draft_contexts[2]["recent_memory"] == ""
    assert memory_path.is_file()
    assert not (project / ".story-system").exists()


def test_successful_memory_round_trips_all_continuity_leaves_into_next_draft(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "memory-round-trip"
    router = SuccessfulMemoryRouter()
    llm = _initialize_and_plan(project, router, monkeypatch)

    first = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 1,
        },
        llm=llm,
    )
    assert first["success"] is True
    memory_path = project / ".webnovel" / "novel_memory.json"
    persisted = json.loads(memory_path.read_text(encoding="utf-8"))["chapters"]["1"]
    assert persisted["summary"] == "SUMMARY-SENTINEL-1"
    assert persisted["character_changes"] == ["CHARACTER-SENTINEL-1"]
    assert persisted["open_threads"] == ["THREAD-SENTINEL-1"]
    assert persisted["ending"] == "ENDING-SENTINEL-1"

    second = runtime.execute_runtime(
        {
            "action": "write_chapter",
            "project_root": str(project),
            "chapter": 2,
        },
        llm=llm,
    )
    assert second["success"] is True
    memory_context = router.draft_contexts[2]["recent_memory"]
    for sentinel in (
        "SUMMARY-SENTINEL-1",
        "CHARACTER-SENTINEL-1",
        "THREAD-SENTINEL-1",
        "ENDING-SENTINEL-1",
    ):
        assert sentinel in memory_context
    continuity = second["outputs"]["model_calls"][0]["continuity"]
    assert all(continuity.values())

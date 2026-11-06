from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from webnovel_studio import live_smoke


def _fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    *,
    mutate_on_review: bool = False,
    model_call_count: int = 3,
    include_continuity: bool = True,
    model_stages: tuple[str, ...] | None = None,
    missing_continuity_key: str | None = None,
) -> list[str]:
    actions: list[str] = []
    selected_stages = model_stages or ("draft", "edit", "memory", "extra")[
        :model_call_count
    ]

    def execute(payload: dict[str, Any], *, llm: Any) -> dict[str, Any]:
        assert llm is not None
        action = str(payload["action"])
        actions.append(action)
        request = payload.get("payload") or {}
        if action == "project_init" and request.get("confirmed") is not True:
            assert not project_root.exists()
            return {
                "success": True,
                "status": "needs_confirmation",
                "outputs": {
                    "proposal": {
                        "title": "旧港回声",
                        "genre": "现实悬疑",
                        "premise": "记者收到失踪者当天寄出的钥匙。",
                        "protagonist": "林舟，独断但敏锐。",
                        "core_conflict": "林舟必须学会相信证人。",
                        "world": "衰败旧港。",
                        "tone": "克制现实",
                        "opening": "湿钥匙送达。",
                        "long_arc": "林舟追查旧港失踪案。",
                    }
                },
            }
        if action == "project_init":
            (project_root / ".webnovel").mkdir(parents=True)
            (project_root / "设定集").mkdir()
            (project_root / "大纲").mkdir()
            (project_root / "正文").mkdir()
            (project_root / "审查报告").mkdir()
            (project_root / ".webnovel" / "state.json").write_text(
                '{"project_info":{"title":"旧港回声"}}\n', encoding="utf-8"
            )
            (project_root / ".webnovel" / "creative_proposal.json").write_text(
                '{"title":"旧港回声"}\n', encoding="utf-8"
            )
            (project_root / "设定集" / "核心设定.md").write_text(
                "# 核心设定\n", encoding="utf-8"
            )
            (project_root / "大纲" / "总纲.md").write_text(
                "# 总纲\n", encoding="utf-8"
            )
            return {
                "success": True,
                "status": "initialized",
                "outputs": {"title": "旧港回声"},
            }
        if action == "project_plan":
            outline = project_root / "大纲" / "第1卷-详细大纲.md"
            outline.write_text(
                "# 第一卷\n\n### 第1章 湿钥匙\n\n- 本章事件：进入旧塔。\n\n"
                "### 第2章 来电\n\n- 本章事件：追查电话。\n",
                encoding="utf-8",
            )
            beat = project_root / "大纲" / "第1卷-节拍表.md"
            beat.write_text("# 第一卷节拍\n", encoding="utf-8")
            return {
                "success": True,
                "status": "planned",
                "outputs": {"outline_file": str(outline), "beat_file": str(beat)},
            }
        if action == "write_chapter":
            number = int(payload["chapter"])
            title = "湿钥匙" if number == 1 else "空号回声"
            body = (
                "林舟把钥匙压在掌心，海水仍从齿缝里渗出来。"
                if number == 1
                else "铃声停下后，林舟接起听筒，听见自己的呼吸。"
            )
            chapter = project_root / "正文" / f"第{number:04d}章-{title}.md"
            chapter.write_text(f"# 第{number}章 {title}\n\n{body}\n", encoding="utf-8")
            memory_path = project_root / ".webnovel" / "novel_memory.json"
            memory = (
                json.loads(memory_path.read_text(encoding="utf-8"))
                if memory_path.exists()
                else {"chapters": {}}
            )
            memory["chapters"][str(number)] = {"summary": body}
            memory_path.write_text(
                json.dumps(memory, ensure_ascii=False), encoding="utf-8"
            )
            return {
                "success": True,
                "status": "written_with_warnings" if number == 2 else "written",
                "outputs": {
                    "chapter_file": str(chapter),
                    "title": title,
                    "character_count": len(body),
                    "warnings": ["target_chars_ignored"] if number == 2 else [],
                    "model_calls": [
                        {
                            "stage": stage,
                            **(
                                {
                                    "continuity": {
                                        key: number > 1 and include_continuity
                                        for key in (
                                            "previous_ending",
                                            "recent_memory",
                                            "memory_character_changes",
                                            "memory_open_threads",
                                            "memory_ending",
                                        )
                                        if key != missing_continuity_key
                                    }
                                }
                                if stage == "draft"
                                else {}
                            ),
                        }
                        for stage in selected_stages
                    ],
                },
            }
        if action == "review_range":
            chapter = project_root / "正文" / "第0001章-湿钥匙.md"
            if mutate_on_review:
                chapter.write_text(
                    chapter.read_text(encoding="utf-8") + "审查擅自改动。\n",
                    encoding="utf-8",
                )
            reports = []
            for number in (1, 2):
                report = project_root / "审查报告" / f"第{number:04d}章-小说质量报告.md"
                report.write_text("# 小说质量报告\n\n对白有潜台词。\n", encoding="utf-8")
                reports.append(str(report))
            return {
                "success": True,
                "status": "reviewed",
                "outputs": {
                    "reports": reports,
                    "warnings": [],
                    "manuscripts_modified": False,
                },
            }
        raise AssertionError(action)

    monkeypatch.setattr(live_smoke, "execute_runtime", execute)
    return actions


def test_live_acceptance_exercises_novel_first_public_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "live-book"
    actions = _fake_runtime(monkeypatch, project)

    result = live_smoke.run_live_acceptance(project, llm=object())

    assert result["success"] is True
    assert result["schema"] == "webnovel-novel-first-live/v2"
    assert actions == [
        "project_init",
        "project_init",
        "project_plan",
        "write_chapter",
        "write_chapter",
        "review_range",
    ]
    assert result["model_call_count"] == 6
    assert [chapter["chapter"] for chapter in result["chapters"]] == [1, 2]
    assert len(result["quality_reports"]) == 2
    assert result["manuscript_sha256_before_review"] == result[
        "manuscript_sha256_after_review"
    ]
    assert result["story_system_created"] is False
    assert not (project / ".story-system").exists()


def test_live_acceptance_requires_a_fresh_absent_project(tmp_path: Path) -> None:
    project = tmp_path / "existing"
    project.mkdir()
    with pytest.raises(live_smoke.LiveAcceptanceError, match="fresh absent"):
        live_smoke.run_live_acceptance(project, llm=object())


def test_live_acceptance_rejects_review_that_changes_manuscript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "mutating-review"
    _fake_runtime(monkeypatch, project, mutate_on_review=True)
    with pytest.raises(live_smoke.LiveAcceptanceError, match="modified the manuscript"):
        live_smoke.run_live_acceptance(project, llm=object())


def test_live_acceptance_enforces_exact_three_stage_model_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "too-many-calls"
    _fake_runtime(monkeypatch, project, model_call_count=4)
    with pytest.raises(live_smoke.LiveAcceptanceError, match="exact draft/edit/memory"):
        live_smoke.run_live_acceptance(project, llm=object())


def test_live_acceptance_rejects_three_calls_in_wrong_stage_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "wrong-stage-order"
    _fake_runtime(
        monkeypatch,
        project,
        model_stages=("draft", "memory", "edit"),
    )
    with pytest.raises(live_smoke.LiveAcceptanceError, match="exact draft/edit/memory"):
        live_smoke.run_live_acceptance(project, llm=object())


@pytest.mark.parametrize(
    "missing_key",
    [
        "previous_ending",
        "recent_memory",
        "memory_character_changes",
        "memory_open_threads",
        "memory_ending",
    ],
)
def test_live_acceptance_requires_complete_chapter_two_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_key: str
) -> None:
    project = tmp_path / f"missing-{missing_key}"
    _fake_runtime(monkeypatch, project, missing_continuity_key=missing_key)

    with pytest.raises(live_smoke.LiveAcceptanceError, match="prior-story continuity"):
        live_smoke.run_live_acceptance(project, llm=object())
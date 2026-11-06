from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPOSITORY_ROOT / "webnovel_studio" / "skills"
EXPECTED_SKILLS = {
    "webnovel-init",
    "webnovel-plan",
    "webnovel-write",
    "webnovel-review",
    "webnovel-query",
    "webnovel-learn",
    "webnovel-dashboard",
    "webnovel-doctor",
}
CREATIVE_V2_SKILLS = {
    "webnovel-init",
    "webnovel-plan",
    "webnovel-write",
    "webnovel-review",
}
FORBIDDEN_CLAUDE_SURFACE = (
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PROJECT_DIR",
    "Agent tool",
    "AskUserQuestion",
    "allowed-tools: Read",
    "allowed-tools: Write",
    "allowed-tools: Edit",
    "allowed-tools: Grep",
    "allowed-tools: Bash",
    "WebSearch",
    "WebFetch",
)
FORBIDDEN_EXECUTION_PATHS = (
    "SCRIPTS_DIR",
    "SKILL_ROOT",
    "webnovel.py",
    "reference_search.py",
    "python -",
    "```bash",
    "```sh",
)


def _skill_paths() -> dict[str, Path]:
    return {
        path.parent.name: path
        for path in SKILLS_ROOT.glob("*/SKILL.md")
        if path.is_file()
    }


def _parse_skill(path: Path) -> tuple[dict[str, Any], str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n(?P<body>.+)\Z", text, re.DOTALL)
    assert match is not None, f"{path} has malformed or empty frontmatter/body"
    frontmatter = yaml.safe_load(match.group("frontmatter"))
    assert isinstance(frontmatter, dict)
    return frontmatter, match.group("body"), text


def test_exactly_eight_hermes_webnovel_skills_are_ported() -> None:
    assert set(_skill_paths()) == EXPECTED_SKILLS


def test_skill_frontmatter_matches_current_hermes_contract() -> None:
    for directory_name, path in _skill_paths().items():
        frontmatter, body, _ = _parse_skill(path)
        assert frontmatter["name"] == directory_name
        assert isinstance(frontmatter["description"], str)
        assert 0 < len(frontmatter["description"]) <= 1024
        expected_version = "2.0.0" if directory_name in CREATIVE_V2_SKILLS else "1.0.0"
        assert frontmatter["version"] == expected_version
        assert frontmatter["license"] == "GPL-3.0-only"
        assert frontmatter["platforms"] == ["linux", "macos", "windows"]
        assert frontmatter["allowed-tools"] == ["webnovel_runtime"]
        assert frontmatter["metadata"]["hermes"]["requires_tools"] == [
            "webnovel_runtime"
        ]
        assert frontmatter["metadata"]["hermes"]["tags"]
        assert body.strip()


def test_skills_expose_only_the_hermes_runtime_execution_path() -> None:
    for path in _skill_paths().values():
        _, _, text = _parse_skill(path)
        assert "webnovel_runtime" in text
        for forbidden in FORBIDDEN_CLAUDE_SURFACE + FORBIDDEN_EXECUTION_PATHS:
            assert forbidden not in text, f"{path} contains forbidden surface {forbidden!r}"


def test_core_creation_skills_do_not_reintroduce_field_gate_arbitration() -> None:
    init = _parse_skill(_skill_paths()["webnovel-init"])[1]
    plan = _parse_skill(_skill_paths()["webnovel-plan"])[1]
    review = _parse_skill(_skill_paths()["webnovel-review"])[1]
    assert "最多请求一次总确认" in init
    assert "不逐项追问" in init
    assert "每条作者指令只调用一次 `webnovel_runtime`" in plan
    assert "不决定正文是否“通过”" in review


def test_each_skill_keeps_success_and_author_report_semantics() -> None:
    for name, path in _skill_paths().items():
        _, body, _ = _parse_skill(path)
        assert "## 成功标准" in body
        assert "## 作者友好最终报告" in body
        if name in CREATIVE_V2_SKILLS:
            assert "绝对路径" in body
            continue
        assert "总状态：" in body
        assert "一、产生的文件与完成情况" in body
        assert "二、过程中遇到的问题与异常耗时" in body
        assert "三、下一步建议" in body


def test_each_command_maps_to_an_existing_runtime_action() -> None:
    expected_actions = {
        "webnovel-init": ("project_init",),
        "webnovel-plan": ("project_plan",),
        "webnovel-write": ("write_chapter",),
        "webnovel-review": ("review_range",),
        "webnovel-query": ("query",),
        "webnovel-learn": ("query",),
        "webnovel-dashboard": ("dashboard_start",),
        "webnovel-doctor": ("project_status", "doctor"),
    }
    for name, actions in expected_actions.items():
        _, body, _ = _parse_skill(_skill_paths()[name])
        for action in actions:
            assert f"`{action}`" in body


def test_write_is_one_runtime_call_and_quality_is_novel_only() -> None:
    _, body, _ = _parse_skill(_skill_paths()["webnovel-write"])
    assert "每条作者指令只调用一次 `webnovel_runtime`" in body
    assert "action 固定为 `write_chapter`" in body
    assert "字数" in body and "不是小说质量" in body
    for dimension in ("剧情", "对白", "节奏", "情绪", "人物声音", "文学表达", "章末"):
        assert dimension in body
    assert "不得调用 `write_prepare`" in body
    for legacy_action in (
        "write_prepare",
        "write_draft",
        "write_review",
        "write_revise",
        "write_commit",
    ):
        assert f"`{legacy_action}`" in body
    assert "不得要求或转述 `run_id`" in body


def test_init_uses_one_proposal_confirmation_and_only_human_story_files() -> None:
    _, body, _ = _parse_skill(_skill_paths()["webnovel-init"])

    assert "只问一次是否采用" in body
    assert "不要把提案拆成多轮字段问答" in body
    assert "`设定集/核心设定.md`" in body
    assert "`大纲/总纲.md`" in body
    assert "不生成 Story System" in body


def test_plan_keeps_only_writable_chapter_cards_and_supplements_missing_chapters() -> None:
    _, body, _ = _parse_skill(_skill_paths()["webnovel-plan"])

    assert "每条作者指令只调用一次 `webnovel_runtime`" in body
    for required in ("本章事件", "人物选择", "结尾推动"):
        assert required in body
    assert "只补缺失章节" in body
    for obsolete in ("CBN", "CPN", "CEN", "source_id", "Story System"):
        assert obsolete in body

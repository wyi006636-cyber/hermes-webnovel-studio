from __future__ import annotations

import pytest

from webnovel_studio.roles import ROLE_PROMPT_NAMES, load_role_prompt


FORBIDDEN_IMPLEMENTATION_DETAILS = (
    ".webnovel",
    ".story-system",
    "state.json",
    "index.db",
    "sqlite",
    "create table",
    "insert into",
    "webnovel.py",
)


def test_role_loader_accepts_only_the_fixed_utf8_role_names() -> None:
    assert ROLE_PROMPT_NAMES == (
        "architect",
        "context-curator",
        "writer",
        "reader-editor",
    )

    architect = load_role_prompt("architect")
    curator = load_role_prompt("context-curator")
    writer = load_role_prompt("writer")
    reader = load_role_prompt("reader-editor")

    assert "Novel Architect" in architect
    assert "Story Memory Curator" in curator
    assert "Novelist" in writer
    assert "Literary Editor" in reader
    assert "人物" in architect and "冲突" in architect
    assert "下一章" in curator
    assert "正文" in writer


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Architect",
        "context_curator",
        "architect.md",
        "../architect",
        "roles/architect",
        "roles\\architect",
        "/architect",
        "context-curator/../../architect",
    ],
)
def test_role_loader_rejects_aliases_extensions_and_path_traversal(name: str) -> None:
    with pytest.raises((KeyError, ValueError)):
        load_role_prompt(name)


def test_architect_builds_writable_story_instead_of_workflow_contracts() -> None:
    prompt = load_role_prompt("architect")
    folded = prompt.casefold()

    for required in ("人物欲望", "人物缺陷", "关系张力", "核心冲突", "开篇事件", "长期故事动力"):
        assert required in prompt
    assert "每一章都必须发生具体事件" in prompt
    assert "人物做出会带来后果的选择" in prompt
    assert "不要因为作者没给某个字段而停下" in prompt

    for forbidden in FORBIDDEN_IMPLEMENTATION_DETAILS:
        assert forbidden not in folded
    assert "source_id" not in folded
    assert "不要生成机器验收规则、节点编码、逐字锚点、审查门、合同树、索引或投影要求" in prompt

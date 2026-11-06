from __future__ import annotations

import json
from pathlib import Path

from webnovel_studio.novel_context import NovelContextBuilder


def make_project(root: Path) -> Path:
    (root / ".webnovel").mkdir(parents=True)
    (root / "大纲").mkdir()
    (root / "设定集").mkdir()
    (root / "正文").mkdir()
    (root / ".webnovel" / "state.json").write_text(
        json.dumps(
            {
                "project_info": {
                    "title": "潮汐尽头",
                    "genre": "科幻",
                    "premise": "失去记忆的领航员寻找失踪舰队。",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "大纲" / "总纲.md").write_text(
        "# 总纲\n\n林雾必须在潮汐风暴抵达前找到舰队。\n",
        encoding="utf-8",
    )
    (root / "大纲" / "第1卷-详细大纲.md").write_text(
        "# 第一卷\n\n### 第1章 失火的信标\n\n林雾登上废弃信标站，发现妹妹留下的航线。\n\n"
        "### 第2章 沉默航道\n\n林雾沿航线进入禁区，旧搭档周野隐瞒了坐标来源。\n",
        encoding="utf-8",
    )
    (root / "设定集" / "人物.md").write_text(
        "# 人物\n\n林雾：克制、敏锐，不轻易相信承诺。\n\n周野：嘴硬，遇到危险先保护林雾。\n",
        encoding="utf-8",
    )
    return root


def test_context_uses_human_outline_settings_memory_and_previous_ending(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    (root / "正文" / "第0001章-失火的信标.md").write_text(
        "# 第1章 失火的信标\n\n" + "风暴压住信标。" * 100 + "坐标在她掌心亮起。",
        encoding="utf-8",
    )
    (root / ".webnovel" / "novel_memory.json").write_text(
        json.dumps(
            {
                "chapters": {
                    "1": {
                        "summary": "林雾取得妹妹留下的航线。",
                        "character_changes": ["林雾开始怀疑周野"],
                        "open_threads": ["坐标为何指向禁区"],
                        "ending": "坐标在她掌心亮起。",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    context = NovelContextBuilder(root).build(2, author_instruction="对白要克制")

    assert context.book_title == "潮汐尽头"
    assert context.chapter == 2
    assert context.title == "沉默航道"
    assert "旧搭档周野隐瞒了坐标来源" in context.chapter_outline
    assert "林雾：克制、敏锐" in context.setting_context
    assert "林雾取得妹妹留下的航线" in context.recent_memory
    assert context.previous_ending.endswith("坐标在她掌心亮起。")
    assert context.author_instruction == "对白要克制"


def test_missing_contracts_indexes_and_memory_are_warnings_not_blockers(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")

    context = NovelContextBuilder(root).build(1)

    assert context.chapter == 1
    assert context.title == "失火的信标"
    assert "memory_missing" in context.warnings
    assert not (root / ".story-system").exists()
    assert not (root / ".webnovel" / "index.db").exists()


def test_missing_or_invalid_state_is_a_warning_not_a_prose_gate(tmp_path: Path) -> None:
    missing = make_project(tmp_path / "missing-state")
    (missing / ".webnovel" / "state.json").unlink()

    missing_context = NovelContextBuilder(missing).build(1)

    assert "state_missing" in missing_context.warnings
    assert "林雾登上废弃信标站" in missing_context.chapter_outline
    assert "林雾：克制、敏锐" in missing_context.setting_context

    invalid = make_project(tmp_path / "invalid-state")
    (invalid / ".webnovel" / "state.json").write_text("{broken", encoding="utf-8")

    invalid_context = NovelContextBuilder(invalid).build(1)

    assert "state_invalid" in invalid_context.warnings
    assert "林雾登上废弃信标站" in invalid_context.chapter_outline


def test_confirmed_settings_have_explicit_priority_over_conflicting_generated_context(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    (root / "设定集" / "人物.md").write_text(
        "# 作者确认设定\n\n林雾绝不饮酒。\n",
        encoding="utf-8",
    )
    (root / "大纲" / "第1卷-详细大纲.md").write_text(
        "# 第一卷\n\n### 第1章 酒杯\n\n模型草案声称林雾必须饮酒。\n",
        encoding="utf-8",
    )
    (root / ".webnovel" / "novel_memory.json").write_text(
        json.dumps(
            {"chapters": {"0": {"summary": "旧记忆声称林雾每天饮酒。"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prompt = NovelContextBuilder(root).build(1).as_prompt()

    assert "林雾绝不饮酒" in prompt["setting_context"]
    assert prompt["authority_order"][0] == "setting_context"
    assert "设定集" in prompt["authority_rule"]
    assert "冲突" in prompt["authority_rule"]


def test_oversized_context_keeps_chapter_relevant_confirmed_settings_within_limits(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    (root / "大纲" / "第1卷-详细大纲.md").write_text(
        "### 第1章 黑潮钥匙\n\n林雾发现黑潮钥匙，必须判断它是否会灼伤持有者。\n",
        encoding="utf-8",
    )
    (root / "设定集" / "人物.md").write_text(
        "无关港口背景。" * 2000, encoding="utf-8"
    )
    (root / "设定集" / "最相关确认设定.md").write_text(
        "作者确认：黑潮钥匙冰冷，不会灼伤持有者。\n" + "相关细节。" * 200,
        encoding="utf-8",
    )
    (root / ".webnovel" / "novel_memory.json").write_text(
        json.dumps(
            {
                "chapters": {
                    "0": {
                        "summary": "旧摘要" * 2000,
                        "character_changes": ["旧变化" * 1000],
                        "open_threads": ["旧伏笔" * 1000],
                        "ending": "旧结尾" * 1000,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    context = NovelContextBuilder(
        root,
        outline_limit=180,
        setting_limit=160,
        memory_limit=120,
        previous_ending_limit=80,
    ).build(1)

    assert "黑潮钥匙冰冷，不会灼伤" in context.setting_context
    assert len(context.setting_context) <= 160
    assert len(context.chapter_outline) <= 180
    assert len(context.book_context) <= 180
    assert len(context.recent_memory) <= 120

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from webnovel_studio.creative_actions import CreativeProjectService
from webnovel_studio.de_ai_linter import (
    build_scene_beats,
    classify_chapter_hook,
    detect_language,
    lint_prose,
)
from webnovel_studio.novel_context import NovelContextBuilder
from webnovel_studio.novel_quality_service import NovelQualityService
from webnovel_studio.novel_writing_service import NovelWritingService


class UpgradeRouter:
    def __init__(self, *, language: str = "zh") -> None:
        self.language = language
        self.edit_payloads: list[dict[str, Any]] = []
        self.draft_payloads: list[dict[str, Any]] = []

    def complete(self, role: str, messages: list[dict[str, str]], *, purpose: str = "") -> Any:
        del role
        if purpose == "webnovel:novel:draft":
            payload = json.loads(messages[-1]["content"])
            self.draft_payloads.append(payload)
            if self.language == "en":
                return SimpleNamespace(
                    text=(
                        "Kael stepped onto the obsidian bridge. A shiver ran down his spine as the ancient "
                        "runes flared to life, a testament to the fallen empire.\n\n"
                        '"Stay back," Lyra whispered, her blade drawn against the encroaching shadow. '
                        "Below them, an impossible signal echoed from the abyss?"
                    )
                )
            return SimpleNamespace(
                text=(
                    "林雾推开信标站沉重的铁门，嘴角勾起一抹冷笑，眼神微眯地看向控制台。"
                    "周野站在阴影里倒吸一口凉气。\n\n"
                    "“别碰那根红线！”周野压低声音警告。就在舱门锁死的瞬间，铁门外突然响起急促的敲击声，门外究竟是谁？"
                )
            )
        if purpose == "webnovel:novel:edit":
            payload = json.loads(messages[-1]["content"])
            self.edit_payloads.append(payload)
            if self.language == "en":
                return SimpleNamespace(
                    text=(
                        "Kael stepped onto the obsidian bridge as cold wind bit through his cloak and the ancient "
                        "runes flared crimson.\n\n"
                        '"Stay back," Lyra whispered, raising her blade as an impossible signal echoed from the abyss?'
                    )
                )
            return SimpleNamespace(
                text=(
                    "林雾推开信标站沉重的铁门，目光锁定积灰的控制台。周野在阴影里猛地按住刀柄。\n\n"
                    "“别碰那根红线！”周野压低声音警告。舱门锁死的瞬间，铁门外突然响起三声急促的敲击，门外究竟是谁？"
                )
            )
        if purpose == "webnovel:novel:quality-review":
            return SimpleNamespace(
                text="## 有效之处\n场景动作扎实。\n\n## 可以提升\n可进一步收紧对白。\n\n## 可直接尝试的修改\n精简转场。"
            )
        raise AssertionError(purpose)

    def complete_structured(self, role: str, **kwargs: Any) -> Any:
        del role
        purpose = str(kwargs.get("purpose") or "")
        if purpose == "webnovel:creative:init":
            if self.language == "en":
                return SimpleNamespace(
                    parsed={
                        "title": "Ashes of the Astral Gate",
                        "genre": "Progression Fantasy",
                        "premise": "An exiled rune-smith rebuilds a shattered star-gate before the void tide arrives.",
                        "protagonist": "Kael: relentless rune-smith who refuses to abandon his sworn companion.",
                        "core_conflict": "Kael must forge forbidden runes while hunted by the High Synod.",
                        "world": "Shattered sky-citadels linked by dying astral bridges.",
                        "tone": "Tense, atmospheric, character-driven",
                        "opening": "A dormant astral beacon transmits Kael's erased true name.",
                        "long_arc": "Kael ascends the rune tiers and uncovers why the gates were sabotaged.",
                    }
                )
            return SimpleNamespace(
                parsed={
                    "title": "潮汐信标",
                    "genre": "星际悬疑",
                    "premise": "失忆领航员在风暴禁区追查消失的第七舰队。",
                    "protagonist": "林雾：冷静敏锐的领航员，绝不轻信任何口头承诺。",
                    "core_conflict": "林雾必须与隐瞒身份的旧搭档周野合作解开信标密码。",
                    "world": "被引力风暴割裂的边缘星域与废弃信标链。",
                    "tone": "冷峻克制、悬念紧凑",
                    "opening": "停机十年的零号信标突然点名呼叫林雾。",
                    "long_arc": "林雾顺着信标链找回舰队真相并直面背叛者。",
                }
            )
        if purpose == "webnovel:creative:plan":
            if self.language == "en":
                return SimpleNamespace(
                    parsed={
                        "volume_title": "The Obsidian Bridge",
                        "volume_arc": "Kael and Lyra breach the first sealed gate.",
                        "chapter_cards": [
                            {
                                "chapter": 1,
                                "title": "The Erased Signal",
                                "event": "Kael and Lyra enter the sealed obsidian bridge.",
                                "character_choice": "Kael activates the rune core despite the Synod alarm.",
                                "ending_drive": "An impossible signal answers from beneath the abyss.",
                            }
                        ],
                    }
                )
            return SimpleNamespace(
                parsed={
                    "volume_title": "零号呼叫",
                    "volume_arc": "林雾与周野潜入零号信标站。",
                    "chapter_cards": [
                        {
                            "chapter": 1,
                            "title": "铁门后的敲击",
                            "event": "林雾与周野进入零号信标站，发现黑潮钥匙插在控制台上。",
                            "character_choice": "林雾决定拔出黑潮钥匙并封锁退路。",
                            "ending_drive": "锁死的铁门外响起失踪妹妹专用的三短一长敲击声。",
                        },
                        {
                            "chapter": 2,
                            "title": "旧港暗码",
                            "event": "林雾核对敲击暗码，周野被迫承认曾见过她妹妹。",
                            "character_choice": "林雾选择不开门，先通过监控确认来者真伪。",
                            "ending_drive": "监控画面里的人戴着第七舰队指挥官面具。",
                        },
                        {
                            "chapter": 3,
                            "title": "深海回声",
                            "event": "林雾再次拿起黑潮钥匙，尝试启动深海频段。",
                            "character_choice": "林雾押上自己的领航权限强行解码。",
                            "ending_drive": "频段那头传出零号舰的黑匣子录音。",
                        },
                    ],
                }
            )
        if purpose == "webnovel:novel:memory":
            raw = json.loads(kwargs["input"][0]["text"])
            ch = int(raw["chapter"])
            if self.language == "en":
                return SimpleNamespace(
                    parsed={
                        "chapter": ch,
                        "title": raw["title"],
                        "summary": "Kael and Lyra awaken the obsidian bridge runes and hear an abyssal signal.",
                        "character_changes": ["Kael channels the forbidden rune core", "Lyra guards the bridge approach"],
                        "characters": ["Kael", "Lyra"],
                        "location": "Obsidian Bridge",
                        "open_threads": ["Who is transmitting from beneath the abyss?"],
                        "strand": "quest",
                        "tension_score": 9,
                        "ending": "An impossible signal echoed from the abyss.",
                    }
                )
            return SimpleNamespace(
                parsed={
                    "chapter": ch,
                    "title": raw["title"],
                    "summary": f"第{ch}章：林雾与周野在零号信标站拿到黑潮钥匙并听到门外敲击声。",
                    "character_changes": ["林雾拔出黑潮钥匙", "周野按刀戒备"],
                    "characters": ["林雾", "周野"],
                    "location": "零号信标站",
                    "open_threads": [f"第{ch}章悬念：门外敲击者究竟是谁"],
                    "strand": "quest",
                    "tension_score": 9,
                    "ending": "铁门外突然响起三声急促的敲击。",
                }
            )
        raise AssertionError(purpose)


def test_bilingual_de_ai_linter_catches_zh_and_en_cliches_and_hooks() -> None:
    zh_draft = (
        "林雾嘴角勾起一抹弧度，眼神微眯地望着远方。周围众人倒吸一口凉气，心中掀起惊涛骇浪。\n\n"
        "铁门外突然传来急促的脚步声与警报，来人究竟是谁？"
    )
    zh_lint = lint_prose(zh_draft)
    assert zh_lint["language"] == "zh"
    assert zh_lint["cliche_count"] >= 4
    phrases = {item["phrase"] for item in zh_lint["cliche_hits"]}
    assert {"嘴角勾起", "眼神微眯", "倒吸一口凉气", "心中掀起惊涛骇浪"}.issubset(phrases)
    assert zh_lint["hook"]["hook_strength"] == "strong"
    assert zh_lint["edit_directives"]

    en_draft = (
        "A shiver ran down his spine as he gazed at the ruins, a testament to a forgotten age. "
        "He couldn't help but let out a breath across the tapestry of shadows.\n\n"
        "A blade flashed in the dark as the alarm screamed—who was standing behind the door?"
    )
    en_lint = lint_prose(en_draft)
    assert en_lint["language"] == "en"
    assert en_lint["cliche_count"] >= 4
    en_phrases = {item["phrase"] for item in en_lint["cliche_hits"]}
    assert {"a testament to", "shiver ran down", "couldn't help but", "tapestry of"}.issubset(
        en_phrases
    )
    assert en_lint["hook"]["hook_strength"] == "strong"
    assert classify_chapter_hook(en_draft, language="en")["tension_score"] >= 8


def test_full_pipeline_syncs_state_json_and_sqlite_dashboard_and_recalls_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "studio-book"
    router = UpgradeRouter(language="zh")
    creative = CreativeProjectService(root, router=router)
    proposal = creative.propose("失忆领航员寻找消失舰队")
    creative.initialize(proposal)
    creative.plan_volume(1, start_chapter=1, end_chapter=3)

    # Add an extra character in 设定集 to verify Codex auto-detection
    (root / "设定集" / "人物.md").write_text(
        "# 人物设定\n\n- **林雾**：零号领航员，冷静克制。\n- **周野**：旧港机械师，随身携带黑潮扳手。\n",
        encoding="utf-8",
    )

    writer = NovelWritingService(root, router=router)
    ch1 = writer.write_chapter(1)
    assert ch1["status"] == "written"
    assert len(ch1["scene_beats"]) == 4
    assert "林雾" in ch1["active_entities"]
    assert "周野" in ch1["active_entities"]
    # Verify draft clichés were sent to editor and cleaned in final prose
    assert router.edit_payloads[0]["de_ai_lint"]["cliche_count"] >= 3
    assert ch1["quality_lint"]["cliche_count"] == 0

    ch2 = writer.write_chapter(2)
    assert ch2["status"] == "written"

    # Build context for Chapter 3 and verify Codex entities, Foreshadowing Alerts, and Local FTS5/BM25 Recall
    ctx3 = NovelContextBuilder(root).build(3, author_instruction="强化周野与林雾的对峙")
    entity_names = {item["name"] for item in ctx3.active_entities}
    assert {"林雾", "周野"}.issubset(entity_names)
    assert any("敲击者" in item["content"] for item in ctx3.foreshadowing_alerts)
    assert ctx3.retrieved_passages, "Expected chapter 1 passage recall for 黑潮钥匙"
    assert ctx3.retrieved_passages[0]["chapter"] == 1

    # Run quality review and verify review_metrics sync in index.db
    review = NovelQualityService(root, router=router).review_range(1, 2)
    assert review["status"] == "reviewed"

    # Inspect state.json and index.db for Dashboard readiness
    state = json.loads((root / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    assert state["progress"]["current_chapter"] == 2
    assert state["progress"]["total_words"] > 0
    assert "角色" in state["entities_v3"]
    assert state["plot_threads"]["foreshadowing"]
    assert len(state["strand_tracker"]["history"]) == 2
    assert "0001" in state["chapter_meta"]

    db_path = root / ".webnovel" / "index.db"
    assert db_path.is_file()
    with closing(sqlite3.connect(str(db_path))) as conn:
        chapters = conn.execute("SELECT chapter, title, word_count FROM chapters ORDER BY chapter").fetchall()
        assert len(chapters) == 2
        assert chapters[0][2] > 0
        entities = conn.execute("SELECT canonical_name FROM entities").fetchall()
        entity_set = {row[0] for row in entities}
        assert "林雾" in entity_set and "周野" in entity_set
        rels = conn.execute("SELECT from_entity, to_entity FROM relationships").fetchall()
        assert len(rels) >= 1
        rp = conn.execute("SELECT chapter, hook_strength FROM chapter_reading_power").fetchall()
        assert len(rp) == 2
        rm = conn.execute("SELECT end_chapter, overall_score FROM review_metrics").fetchall()
        assert len(rm) == 2


def test_bilingual_english_workflow_auto_detects_and_runs_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "english-book"
    router = UpgradeRouter(language="en")
    creative = CreativeProjectService(root, router=router)
    proposal = creative.propose(
        "Write an English progression fantasy about an exiled rune-smith rebuilding a shattered astral gate."
    )
    assert detect_language(proposal["title"], proposal["premise"]) == "en"
    creative.initialize(proposal)
    creative.plan_volume(1, start_chapter=1, end_chapter=1)

    writer = NovelWritingService(root, router=router)
    result = writer.write_chapter(1, target_chars=2500)
    assert result["status"] == "written"
    assert result["language"] == "en"
    assert result["scene_beats"][0]["phase"] == "Scene Entry & Sensory Anchor"
    assert "Write the chapter prose in English" in router.draft_payloads[0]["instruction"]
    assert result["quality_lint"]["cliche_count"] == 0
    assert len( build_scene_beats(chapter=1, title="The Erased Signal", chapter_outline="Event: Kael enters.", language="en") ) == 4

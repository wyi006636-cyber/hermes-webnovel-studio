"""Bilingual Scene Beats planner and deterministic De-AI Cliché & Rhythm Linter.

Provides zero-API-key static analysis for Chinese webnovels and English
progression/genre fiction:
1. Language auto-detection (Chinese vs English).
2. Scene Beats decomposition (turning a 3-line chapter card into 3-4 playable
   scene beats with POV, sensory anchor, and tension curve).
3. Anti-AI-cliché scanning, paragraph-opening repetition checks, dialogue/pacing
   diagnostics, and chapter-ending hook classification.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping, Sequence


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")

ZH_AI_CLICHES: tuple[tuple[str, str], ...] = (
    ("嘴角勾起", "改用具体神态动作或直接对话，删去“嘴角勾起一抹…”套话"),
    ("嘴角微扬", "用具体语气或动作细节替代程式化微笑"),
    ("嘴角微微上扬", "用具体语气或动作细节替代程式化微笑"),
    ("眼神微眯", "用人物观察到的具体破绽或细节替代“眼神微眯”"),
    ("双眸微眯", "用人物观察到的具体破绽或细节替代“双眸微眯”"),
    ("眼中闪过一丝", "通过动作停顿或话语锋芒表现心理，不写“眼中闪过一丝…”"),
    ("眸中闪过", "通过动作停顿或话语锋芒表现心理，不写“眸中闪过…”"),
    ("倒吸一口凉气", "描写具体旁观者的失态动作或现场死寂，避免群演齐刷刷倒吸凉气"),
    ("恐怖如斯", "展示力量造成的具体物理破坏或生理压迫感，禁用“恐怖如斯”"),
    ("心中掀起惊涛骇浪", "用具体生理反应（指节发白、呼吸停滞）替代抽象成语"),
    ("心中翻江倒海", "用具体生理反应与即时抉择替代“心中翻江倒海”"),
    ("空气仿佛凝固", "描写环境中具体的声音消失或细微声响放大，不写“空气仿佛凝固”"),
    ("气氛瞬间凝固", "描写人物之间具体的距离、视线与动作僵持"),
    ("仿佛在诉说", "去掉拟人化抒情套话，直接写器物或景物的客观痕迹"),
    ("瞳孔骤缩", "改写为本能的防御动作、后撤半步或视线死死锁定某物"),
    ("瞳孔猛地一缩", "改写为本能的防御动作或视线锁定某物"),
    ("带着一丝不易察觉", "写出被谁从哪个具体细节捕捉到，或直接删去“不易察觉”"),
    ("不知为何", "明确人物直觉的来由，避免模糊甩锅给“不知为何”"),
)

EN_AI_CLICHES: tuple[tuple[str, str], ...] = (
    ("a testament to", "Show physical history or consequence instead of 'a testament to'"),
    ("tapestry of", "Use concrete sensory nouns instead of abstract 'tapestry of' metaphors"),
    ("symphony of", "Replace 'symphony of' with specific sounds and textures"),
    ("shiver ran down", "Describe a specific physical reaction or tactical response"),
    ("let out a breath", "Avoid the 'let out a breath they didn't know they were holding' cliché"),
    ("couldn't help but", "Write the character's action or thought directly without 'couldn't help but'"),
    ("eyes widened", "Show reaction through posture, grip, or dialogue interruption"),
    ("palpable tension", "Ground tension in a concrete sound, silence, or physical distance"),
    ("thick with tension", "Ground tension in a concrete sound, silence, or physical distance"),
    ("a surge of", "Show emotion through visceral action rather than 'a surge of'"),
    ("little did they know", "Maintain tight POV discipline without omniscient spoilers"),
    ("unbeknownst to", "Maintain tight POV discipline without omniscient spoilers"),
    ("time seemed to slow", "Use sharp, fragmented sensory details to convey speed and crisis"),
)


def detect_language(*samples: Any, default: str = "zh") -> str:
    """Detect whether the story context or user input is primarily Chinese or English."""
    combined = " ".join(str(item) for item in samples if item is not None)
    if not combined.strip():
        return default
    cjk_count = len(_CJK_RE.findall(combined))
    latin_words = _LATIN_WORD_RE.findall(combined)
    latin_count = len(latin_words)
    if cjk_count >= 4 and cjk_count * 2 >= latin_count:
        return "zh"
    if latin_count >= 5 and cjk_count == 0:
        return "en"
    if latin_count >= 12 and latin_count > cjk_count * 4:
        return "en"
    if cjk_count > 0:
        return "zh"
    return default


def _extract_outline_parts(chapter_outline: str) -> dict[str, str]:
    event = ""
    choice = ""
    drive = ""
    bullets: list[str] = []
    for raw_line in chapter_outline.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = re.sub(r"^[-*•\d.)\s]+", "", line).strip()
        if not cleaned:
            continue
        lower = cleaned.lower()
        if cleaned.startswith(("本章事件：", "本章事件:")) or lower.startswith("event:"):
            event = cleaned.split(":", 1)[-1].split("：", 1)[-1].strip()
        elif cleaned.startswith(("人物选择：", "人物选择:")) or lower.startswith(
            ("character choice:", "choice:")
        ):
            choice = cleaned.split(":", 1)[-1].split("：", 1)[-1].strip()
        elif cleaned.startswith(("结尾推动：", "结尾推动:")) or lower.startswith(
            ("ending drive:", "cliffhanger:", "ending:")
        ):
            drive = cleaned.split(":", 1)[-1].split("：", 1)[-1].strip()
        else:
            bullets.append(cleaned)

    if not event and bullets:
        event = bullets[0]
    if not choice and len(bullets) > 1:
        choice = bullets[1]
    if not drive and len(bullets) > 2:
        drive = bullets[-1]
    if not event:
        event = chapter_outline.strip()[:240]
    return {
        "event": event,
        "choice": choice,
        "drive": drive,
    }


def build_scene_beats(
    *,
    chapter: int,
    title: str,
    chapter_outline: str,
    author_instruction: str = "",
    active_entities: Sequence[Mapping[str, Any]] = (),
    foreshadowing_alerts: Sequence[Mapping[str, Any]] = (),
    language: str = "zh",
) -> list[dict[str, Any]]:
    """Decompose a chapter outline into 3-4 structured Scene Beats with tension curves."""
    parts = _extract_outline_parts(chapter_outline)
    entity_names = [
        str(item.get("name") or item.get("canonical_name") or "").strip()
        for item in active_entities
        if isinstance(item, Mapping)
        and str(item.get("name") or item.get("canonical_name") or "").strip()
    ]
    pov_lead = entity_names[0] if entity_names else ("Protagonist" if language == "en" else "主角")
    urgent_thread = ""
    if foreshadowing_alerts:
        first_alert = foreshadowing_alerts[0]
        if isinstance(first_alert, Mapping):
            urgent_thread = str(first_alert.get("content") or "").strip()

    if language == "en":
        beats = [
            {
                "beat_index": 1,
                "phase": "Scene Entry & Sensory Anchor",
                "pov": pov_lead,
                "goal": parts["event"] or f"Establish immediate stakes in Chapter {chapter} ({title}).",
                "sensory_anchor": "Ground the opening in concrete environment textures, sound, and physical motion.",
                "tension_score": 5,
            },
            {
                "beat_index": 2,
                "phase": "Escalation & Pressure",
                "pov": pov_lead,
                "goal": (
                    f"Escalate conflict around: {parts['event']}. "
                    + (f"Weave in active thread: {urgent_thread}." if urgent_thread else "Introduce friction or resistance.")
                ).strip(),
                "sensory_anchor": "Use subtext-rich dialogue and shifting physical distance between characters.",
                "tension_score": 7,
            },
            {
                "beat_index": 3,
                "phase": "Crucial Choice & Turn",
                "pov": pov_lead,
                "goal": parts["choice"]
                or f"{pov_lead} makes an irreversible tactical or emotional decision.",
                "sensory_anchor": "Show internal pressure through visceral action, never abstract exposition.",
                "tension_score": 8,
            },
            {
                "beat_index": 4,
                "phase": "Cliffhanger & Forward Drive",
                "pov": pov_lead,
                "goal": parts["drive"]
                or f"End Chapter {chapter} on a concrete revelation or immediate threat.",
                "sensory_anchor": "Cut on a sharp image, line of dialogue, or unanswered anomaly.",
                "tension_score": 9,
            },
        ]
        if author_instruction.strip():
            beats[1]["author_emphasis"] = author_instruction.strip()[:200]
        return beats

    beats = [
        {
            "beat_index": 1,
            "phase": "场景切入与感官锚点",
            "pov": pov_lead,
            "goal": parts["event"] or f"切入第{chapter}章「{title}」的核心现场与当下处境。",
            "sensory_anchor": "用具体动作、环境声响与冷暖触感开场，不写空泛背景说明。",
            "tension_score": 5,
        },
        {
            "beat_index": 2,
            "phase": "冲突升级与暗线施压",
            "pov": pov_lead,
            "goal": (
                f"围绕「{parts['event'] or title}」升级阻力与博弈"
                + (f"，并自然触碰待回收伏笔：{urgent_thread}。" if urgent_thread else "，让对手或环境给出真实反制。")
            ),
            "sensory_anchor": "对白带潜台词与身份差，用微动作和站位变化体现关系张力。",
            "tension_score": 7,
        },
        {
            "beat_index": 3,
            "phase": "人物抉择与破局转折",
            "pov": pov_lead,
            "goal": parts["choice"] or f"{pov_lead}做出有代价的关键选择，推动局面实质改变。",
            "sensory_anchor": "情绪通过生理反应与果断行动落地，拒绝套话式心理独白。",
            "tension_score": 8,
        },
        {
            "beat_index": 4,
            "phase": "章末断章与追读钩子",
            "pov": pov_lead,
            "goal": parts["drive"] or f"在第{chapter}章结尾抛出具体异常、新威胁或未解悬念。",
            "sensory_anchor": "停在最具悬念的具体画面、异响或半句台词上，不写总结陈词。",
            "tension_score": 9,
        },
    ]
    if author_instruction.strip():
        beats[1]["author_emphasis"] = author_instruction.strip()[:200]
    return beats


def classify_chapter_hook(text: str, *, language: str = "zh") -> dict[str, Any]:
    """Classify the chapter ending's hook type and strength."""
    tail = text.strip()[-260:] if text.strip() else ""
    if not tail:
        return {
            "hook_type": "Unclassified" if language == "en" else "常规推进",
            "hook_strength": "weak",
            "tension_score": 5,
            "ending_snippet": "",
        }

    if language == "en":
        crisis_terms = ("blood", "alarm", "gun", "blade", "scream", "footsteps", "trap", "dead", "run", "fire", "knock")
        mystery_terms = ("who", "why", "impossible", "message", "signal", "secret", "name", "voice", "shadow", "?", "—")
        lower_tail = tail.lower()
        has_crisis = any(term in lower_tail for term in crisis_terms)
        has_mystery = any(term in lower_tail for term in mystery_terms)
        if has_crisis and has_mystery:
            return {
                "hook_type": "Crisis + Mystery Hook",
                "hook_strength": "strong",
                "tension_score": 9,
                "ending_snippet": tail[-120:],
            }
        if has_crisis:
            return {
                "hook_type": "Crisis Hook",
                "hook_strength": "strong",
                "tension_score": 8,
                "ending_snippet": tail[-120:],
            }
        if has_mystery:
            return {
                "hook_type": "Suspense Hook",
                "hook_strength": "medium",
                "tension_score": 7,
                "ending_snippet": tail[-120:],
            }
        return {
            "hook_type": "Momentum Hook",
            "hook_strength": "medium",
            "tension_score": 6,
            "ending_snippet": tail[-120:],
        }

    crisis_markers = ("血", "警报", "杀", "死", "刀", "剑", "枪", "爆炸", "包围", "危险", "退路", "碎", "毒")
    suspense_markers = ("？", "?", "是谁", "名字", "声音", "响起", "信标", "秘密", "真相", "居然", "竟然", "不可能", "来电", "敲击")
    has_crisis = any(marker in tail for marker in crisis_markers)
    has_suspense = any(marker in tail for marker in suspense_markers)
    if has_crisis and has_suspense:
        return {
            "hook_type": "危机悬念复合钩",
            "hook_strength": "strong",
            "tension_score": 9,
            "ending_snippet": tail[-100:],
        }
    if has_crisis:
        return {
            "hook_type": "危机钩",
            "hook_strength": "strong",
            "tension_score": 8,
            "ending_snippet": tail[-100:],
        }
    if has_suspense:
        return {
            "hook_type": "悬念钩",
            "hook_strength": "strong" if ("？" in tail or "?" in tail) else "medium",
            "tension_score": 8 if ("？" in tail or "?" in tail) else 7,
            "ending_snippet": tail[-100:],
        }
    return {
        "hook_type": "情绪余韵钩",
        "hook_strength": "medium",
        "tension_score": 6,
        "ending_snippet": tail[-100:],
    }


def lint_prose(text: str, *, language: str | None = None) -> dict[str, Any]:
    """Scan chapter prose for AI clichés, repetitive openings, and pacing balance."""
    lang = language or detect_language(text)
    cliche_bank = EN_AI_CLICHES if lang == "en" else ZH_AI_CLICHES
    lower_text = text.lower() if lang == "en" else text

    cliche_hits: list[dict[str, Any]] = []
    total_cliche_occurrences = 0
    for phrase, fix in cliche_bank:
        needle = phrase.lower() if lang == "en" else phrase
        count = lower_text.count(needle)
        if count > 0:
            total_cliche_occurrences += count
            cliche_hits.append(
                {
                    "phrase": phrase,
                    "count": count,
                    "suggestion": fix,
                }
            )

    paragraphs = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    opening_counter: Counter[str] = Counter()
    for para in paragraphs:
        cleaned = re.sub(r"^[“\"'‘「『（(\s]+", "", para)
        if lang == "en":
            words = _LATIN_WORD_RE.findall(cleaned)
            if len(words) >= 2:
                opening_counter[f"{words[0].lower()} {words[1].lower()}"] += 1
        else:
            cjk = "".join(_CJK_RE.findall(cleaned))
            if len(cjk) >= 2:
                opening_counter[cjk[:2]] += 1

    repetitive_openings = [
        {"opening": prefix, "count": count}
        for prefix, count in opening_counter.most_common(5)
        if count >= 3 and len(paragraphs) >= 5
    ]

    dialogue_paragraphs = sum(
        1
        for para in paragraphs
        if any(mark in para for mark in ("“", "”", '"', "「", "」", "『", "』"))
    )
    paragraph_count = max(1, len(paragraphs))
    dialogue_ratio = round(dialogue_paragraphs / paragraph_count, 2)

    hook = classify_chapter_hook(text, language=lang)
    score = max(
        40,
        100 - min(40, total_cliche_occurrences * 6) - min(20, len(repetitive_openings) * 5),
    )

    directives: list[str] = []
    if cliche_hits:
        for item in cliche_hits[:5]:
            if lang == "en":
                directives.append(
                    f"Remove AI cliché '{item['phrase']}' ({item['count']}x): {item['suggestion']}"
                )
            else:
                directives.append(
                    f"消除 AI 套话「{item['phrase']}」（出现 {item['count']} 次）：{item['suggestion']}"
                )
    if repetitive_openings:
        prefixes = ", ".join(f"'{item['opening']}'" for item in repetitive_openings[:3])
        if lang == "en":
            directives.append(
                f"Vary repetitive paragraph openings ({prefixes}) with concrete actions or sensory shifts."
            )
        else:
            directives.append(
                f"打散重复的段落起手词（{prefixes}），改用环境切镜、动作反应或直接对话起句。"
            )
    if hook["hook_strength"] == "weak":
        if lang == "en":
            directives.append(
                "Sharpen the final paragraph into a concrete cliffhanger or unresolved dramatic turn."
            )
        else:
            directives.append("强化章末最后一段的断章悬念，停在具体异变、抉择或未完对话上。")

    return {
        "language": lang,
        "freshness_score": score,
        "cliche_count": total_cliche_occurrences,
        "cliche_hits": cliche_hits,
        "repetitive_openings": repetitive_openings,
        "paragraph_count": len(paragraphs),
        "dialogue_ratio": dialogue_ratio,
        "hook": hook,
        "edit_directives": directives,
    }


__all__ = [
    "EN_AI_CLICHES",
    "ZH_AI_CLICHES",
    "build_scene_beats",
    "classify_chapter_hook",
    "detect_language",
    "lint_prose",
]

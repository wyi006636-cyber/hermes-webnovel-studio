"""Post-chapter story state extraction, Dashboard SQLite sync, and zero-API-key retrieval.

Bridges the human-first novel writing pipeline with:
1. `.webnovel/state.json` (entities_v3, plot_threads.foreshadowing, strand_tracker,
   chapter_meta, protagonist_state, progress.total_words).
2. `.webnovel/index.db` (SQLite tables read by the FastAPI + React Web Dashboard
   plus an FTS5 full-text search index `chapter_fts`).
3. Read-only Codex entity auto-detection, foreshadowing debt alerts, and local
   BM25/FTS5 passage recall for `NovelContextBuilder`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .de_ai_linter import classify_chapter_hook, detect_language, lint_prose


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CHAPTER_FILE_RE = re.compile(
    r"^(?:第0*(\d+)章|Chapter[-_\s]*0*(\d+))(?:[-_\s—:：]*(.*))?\.md$",
    re.IGNORECASE,
)
_SETTING_ENTITY_LINE_RE = re.compile(
    r"^(?:[-*•]\s*)?(?:\*\*([^*]{1,24})\*\*|([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·\s]{0,20}))\s*[：:]\s*(.+)$"
)
_STOP_ENTITY_NAMES = frozenset(
    {
        "题材",
        "调性",
        "主角",
        "世界",
        "核心冲突",
        "核心设定",
        "一句话故事",
        "开篇事件",
        "长期故事动力",
        "本章事件",
        "人物选择",
        "结尾推动",
        "作者确认",
        "注意",
        "说明",
        "备注",
        "genre",
        "tone",
        "protagonist",
        "world",
        "core conflict",
        "premise",
        "opening",
        "long arc",
        "event",
        "character choice",
        "ending drive",
    }
)


def _slug_id(name: str, prefix: str = "ent") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip()).strip("_").lower()
    if cleaned and len(cleaned) >= 2:
        return f"{prefix}_{cleaned[:24]}"
    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _count_prose_words(text: str) -> int:
    body_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    body = "\n".join(body_lines)
    cjk = len(_CJK_RE.findall(body))
    latin = len(_LATIN_WORD_RE.findall(body))
    if cjk > 0 and latin > 0:
        return cjk + latin
    if cjk > 0:
        return len(re.sub(r"\s+", "", body))
    if latin > 0:
        return latin
    return len(body)


def _extract_entities_from_settings_markdown(text: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    current_section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_section = line.lstrip("#").strip()
            continue
        match = _SETTING_ENTITY_LINE_RE.match(line)
        if not match:
            if "主角" in current_section or "protagonist" in current_section.lower():
                head = re.split(r"[，,。；;：:—-]", line, maxsplit=1)[0].strip()
                if 1 < len(head) <= 18 and head.lower() not in _STOP_ENTITY_NAMES:
                    if head not in seen_names:
                        seen_names.add(head)
                        entities.append(
                            {
                                "id": _slug_id(head, "char"),
                                "type": "角色",
                                "name": head,
                                "canonical_name": head,
                                "tier": "核心",
                                "desc": line[:200],
                                "aliases": [],
                                "is_protagonist": True,
                                "current": {"status": line[:120]},
                            }
                        )
            continue

        raw_name = (match.group(1) or match.group(2) or "").strip()
        desc = (match.group(3) or "").strip()
        if not raw_name or raw_name.lower() in _STOP_ENTITY_NAMES or len(raw_name) > 20:
            continue
        if raw_name in seen_names:
            continue
        seen_names.add(raw_name)
        entity_type = "角色"
        lower_sec = current_section.lower()
        if any(k in current_section for k in ("地点", "世界", "地图", "场景")) or any(
            k in lower_sec for k in ("location", "world", "place")
        ):
            entity_type = "地点"
        elif any(k in current_section for k in ("物品", "法宝", "道具", "武器")) or any(
            k in lower_sec for k in ("item", "artifact", "relic", "weapon")
        ):
            entity_type = "物品"
        elif any(k in current_section for k in ("势力", "组织", "宗门", "阵营")) or any(
            k in lower_sec for k in ("faction", "guild", "sect")
        ):
            entity_type = "势力"

        is_protag = ("主角" in current_section or "protagonist" in lower_sec) or (
            len(entities) == 0 and entity_type == "角色"
        )
        entities.append(
            {
                "id": _slug_id(raw_name, "char" if entity_type == "角色" else "ent"),
                "type": entity_type,
                "name": raw_name,
                "canonical_name": raw_name,
                "tier": "核心" if is_protag else "重要",
                "desc": desc[:240],
                "aliases": [],
                "is_protagonist": is_protag,
                "current": {"role": desc[:120]},
            }
        )
    return entities


def collect_codex_entities(
    project_root: Path,
    state: Mapping[str, Any],
    relevance_text: str,
    current_chapter: int,
) -> list[dict[str, Any]]:
    """Read-only Codex entity detection across state.json, index.db (if present), and 设定集."""
    by_name: dict[str, dict[str, Any]] = {}

    # 1. From state.json entities_v3
    entities_v3 = state.get("entities_v3")
    if isinstance(entities_v3, Mapping):
        for entity_type, group in entities_v3.items():
            if not isinstance(group, Mapping):
                continue
            for entity_id, info in group.items():
                if not isinstance(info, Mapping):
                    continue
                name = str(info.get("canonical_name") or info.get("name") or entity_id).strip()
                if not name:
                    continue
                aliases = [
                    str(a).strip()
                    for a in (info.get("aliases") or [])
                    if str(a).strip()
                ]
                by_name[name] = {
                    "id": str(entity_id),
                    "type": str(entity_type),
                    "name": name,
                    "canonical_name": name,
                    "tier": str(info.get("tier") or "重要"),
                    "desc": str(info.get("desc") or ""),
                    "aliases": aliases,
                    "is_protagonist": bool(info.get("is_protagonist", False)),
                    "first_appearance": int(info.get("first_appearance") or 1),
                    "last_appearance": int(info.get("last_appearance") or current_chapter),
                    "current": dict(info.get("current") or {})
                    if isinstance(info.get("current"), Mapping)
                    else {},
                }

    # 2. From protagonist_state if not already present
    protag_state = state.get("protagonist_state")
    if isinstance(protag_state, Mapping):
        pname = str(protag_state.get("name") or "").strip()
        if pname and pname not in by_name:
            by_name[pname] = {
                "id": _slug_id(pname, "char"),
                "type": "角色",
                "name": pname,
                "canonical_name": pname,
                "tier": "核心",
                "desc": "",
                "aliases": [],
                "is_protagonist": True,
                "first_appearance": 1,
                "last_appearance": max(1, current_chapter - 1),
                "current": {
                    "location": str(protag_state.get("location") or ""),
                    "power": protag_state.get("power") or {},
                },
            }

    # 3. From 设定集/*.md
    settings_dir = project_root / "设定集"
    if settings_dir.is_dir() and not settings_dir.is_symlink():
        for md_path in sorted(settings_dir.rglob("*.md")):
            if md_path.is_file() and not md_path.is_symlink():
                try:
                    text = md_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                for extracted in _extract_entities_from_settings_markdown(text):
                    name = extracted["name"]
                    if name not in by_name:
                        by_name[name] = extracted
                    elif extracted.get("desc") and not by_name[name].get("desc"):
                        by_name[name]["desc"] = extracted["desc"]

    # 4. From index.db if it already exists on disk (strictly read-only!)
    db_path = project_root / ".webnovel" / "index.db"
    if db_path.is_file() and not db_path.is_symlink():
        try:
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM entities WHERE is_archived = 0 ORDER BY is_protagonist DESC, last_appearance DESC LIMIT 50"
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    name = str(item.get("canonical_name") or item.get("id") or "").strip()
                    if not name:
                        continue
                    try:
                        current_obj = json.loads(item.get("current_json") or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        current_obj = {}
                    if name not in by_name:
                        by_name[name] = {
                            "id": str(item.get("id") or _slug_id(name)),
                            "type": str(item.get("type") or "角色"),
                            "name": name,
                            "canonical_name": name,
                            "tier": str(item.get("tier") or "重要"),
                            "desc": str(item.get("desc") or ""),
                            "aliases": [],
                            "is_protagonist": bool(item.get("is_protagonist")),
                            "first_appearance": int(item.get("first_appearance") or 1),
                            "last_appearance": int(item.get("last_appearance") or 1),
                            "current": current_obj if isinstance(current_obj, dict) else {},
                        }
        except sqlite3.Error:
            pass

    lowered_ref = relevance_text.lower()
    matched: list[tuple[int, dict[str, Any]]] = []
    for name, entity in by_name.items():
        score = 0
        if entity.get("is_protagonist"):
            score += 5
        if name in relevance_text or name.lower() in lowered_ref:
            score += 10
        for alias in entity.get("aliases") or []:
            if len(alias) >= 2 and (alias in relevance_text or alias.lower() in lowered_ref):
                score += 6
        if score > 0:
            matched.append((score, entity))

    matched.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [item for _score, item in matched[:8]]


def collect_foreshadowing_alerts(
    project_root: Path,
    state: Mapping[str, Any],
    current_chapter: int,
) -> list[dict[str, Any]]:
    """Return active foreshadowing threads ranked by urgency for chapter `current_chapter`."""
    alerts: list[dict[str, Any]] = []
    seen_contents: set[str] = set()

    plot_threads = state.get("plot_threads")
    if isinstance(plot_threads, Mapping):
        raw_list = plot_threads.get("foreshadowing")
        if isinstance(raw_list, list):
            for entry in raw_list:
                if not isinstance(entry, Mapping):
                    continue
                status = str(entry.get("status") or "active").lower()
                if status in {"resolved", "closed", "archived"}:
                    continue
                content = str(entry.get("content") or "").strip()
                if not content or content in seen_contents:
                    continue
                seen_contents.add(content)
                planted = int(entry.get("planted_chapter") or 1)
                target = int(entry.get("target_chapter") or (planted + 8))
                elapsed = max(0, current_chapter - planted)
                base_urgency = int(entry.get("urgency") or 40)
                urgency = min(100, max(base_urgency, 30 + elapsed * 10))
                if current_chapter >= target:
                    urgency = max(urgency, 85)
                alerts.append(
                    {
                        "content": content,
                        "planted_chapter": planted,
                        "target_chapter": target,
                        "chapters_elapsed": elapsed,
                        "urgency": urgency,
                        "overdue": current_chapter >= target,
                    }
                )

    memory_path = project_root / ".webnovel" / "novel_memory.json"
    if memory_path.is_file() and not memory_path.is_symlink():
        try:
            mem = json.loads(memory_path.read_text(encoding="utf-8"))
            chapters_mem = mem.get("chapters") if isinstance(mem, Mapping) else {}
            if isinstance(chapters_mem, Mapping):
                for raw_ch, ch_payload in sorted(
                    chapters_mem.items(),
                    key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0,
                ):
                    if not str(raw_ch).isdigit():
                        continue
                    planted = int(raw_ch)
                    if planted >= current_chapter or not isinstance(ch_payload, Mapping):
                        continue
                    for thread in ch_payload.get("open_threads") or []:
                        content = str(thread).strip()
                        if not content or content in seen_contents:
                            continue
                        seen_contents.add(content)
                        elapsed = max(0, current_chapter - planted)
                        target = planted + 8
                        urgency = min(100, 35 + elapsed * 10)
                        alerts.append(
                            {
                                "content": content,
                                "planted_chapter": planted,
                                "target_chapter": target,
                                "chapters_elapsed": elapsed,
                                "urgency": urgency,
                                "overdue": current_chapter >= target,
                            }
                        )
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            pass

    alerts.sort(key=lambda item: (-int(item["urgency"]), int(item["planted_chapter"])))
    return alerts[:6]


def retrieve_local_passages(
    project_root: Path,
    query_text: str,
    *,
    before_chapter: int,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Zero-API-key hybrid retrieval over earlier chapters using SQLite FTS5 or local BM25."""
    if before_chapter <= 2 or not query_text.strip():
        return []

    cjk_chars = "".join(_CJK_RE.findall(query_text))
    bigrams = {
        cjk_chars[i : i + 2]
        for i in range(len(cjk_chars) - 1)
        if cjk_chars[i : i + 2] not in {"本章", "第1", "第2", "章节", "主角", "一个", "没有"}
    }
    words = {w.lower() for w in _LATIN_WORD_RE.findall(query_text) if len(w) >= 3}
    if not bigrams and not words:
        return []

    scored_passages: list[tuple[int, int, str, str]] = []
    db_path = project_root / ".webnovel" / "index.db"
    if db_path.is_file() and not db_path.is_symlink():
        fts_tokens = sorted(words)[:4] + sorted(bigrams)[:4]
        fts_query = " OR ".join(f'"{tok}"' for tok in fts_tokens if tok)
        if fts_query:
            try:
                with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        SELECT chapter, title, summary, substr(body, 1, 280) AS snippet
                        FROM chapter_fts
                        WHERE chapter_fts MATCH ?
                        LIMIT 8
                        """,
                        (fts_query,),
                    ).fetchall()
                    for row in rows:
                        ch_num = int(row["chapter"] or 0)
                        if 0 < ch_num < before_chapter - 1:
                            excerpt = str(row["summary"] or row["snippet"] or "").strip()
                            if excerpt:
                                scored_passages.append(
                                    (10, ch_num, str(row["title"] or f"第{ch_num}章"), excerpt[:260])
                                )
            except sqlite3.Error:
                pass

    body_dir = project_root / "正文"
    if body_dir.is_dir() and not body_dir.is_symlink():
        for path in sorted(body_dir.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            match = _CHAPTER_FILE_RE.match(path.name)
            if not match:
                continue
            ch_num = int(match.group(1) or match.group(2) or 0)
            if ch_num <= 0 or ch_num >= before_chapter - 1:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            paragraphs = [
                p.strip()
                for p in text.split("\n\n")
                if p.strip() and not p.strip().startswith("#")
            ]
            for para in paragraphs:
                lower_p = para.lower()
                score = sum(2 for bg in bigrams if bg in para) + sum(
                    3 for w in words if w in lower_p
                )
                if score >= 4:
                    scored_passages.append((score, ch_num, path.stem, para[:260]))

    scored_passages.sort(key=lambda item: (-item[0], -item[1]))
    results: list[dict[str, Any]] = []
    seen_chapters: set[int] = set()
    for score, ch_num, title, snippet in scored_passages:
        if ch_num in seen_chapters and len(results) >= 1:
            continue
        seen_chapters.add(ch_num)
        results.append(
            {
                "chapter": ch_num,
                "title": title,
                "score": score,
                "excerpt": snippet,
            }
        )
        if len(results) >= limit:
            break
    return results


class StoryStateSync:
    """Synchronize chapter manuscripts, `.webnovel/state.json`, and `.webnovel/index.db`."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.webnovel_dir = self.project_root / ".webnovel"
        self.state_path = self.webnovel_dir / "state.json"
        self.db_path = self.webnovel_dir / "index.db"

    def _init_db_schema(self, conn: sqlite3.Connection) -> None:
        cursor = conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS chapters (
                chapter INTEGER PRIMARY KEY,
                title TEXT,
                location TEXT,
                word_count INTEGER,
                characters TEXT,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter INTEGER,
                scene_index INTEGER,
                start_line INTEGER,
                end_line INTEGER,
                location TEXT,
                summary TEXT,
                characters TEXT,
                UNIQUE(chapter, scene_index)
            );
            CREATE TABLE IF NOT EXISTS appearances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT,
                chapter INTEGER,
                mentions TEXT,
                confidence REAL,
                UNIQUE(entity_id, chapter)
            );
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                tier TEXT DEFAULT '重要',
                desc TEXT,
                current_json TEXT,
                first_appearance INTEGER DEFAULT 0,
                last_appearance INTEGER DEFAULT 0,
                is_protagonist INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (alias, entity_id, entity_type)
            );
            CREATE TABLE IF NOT EXISTS state_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                chapter INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                chapter INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(from_entity, to_entity, type)
            );
            CREATE TABLE IF NOT EXISTS relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_entity TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                type TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'update',
                polarity INTEGER DEFAULT 0,
                strength REAL DEFAULT 0.5,
                description TEXT,
                chapter INTEGER NOT NULL,
                scene_index INTEGER DEFAULT 0,
                evidence TEXT,
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS override_contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter INTEGER NOT NULL,
                constraint_type TEXT NOT NULL,
                constraint_id TEXT NOT NULL,
                rationale_type TEXT NOT NULL,
                rationale_text TEXT,
                payback_plan TEXT,
                due_chapter INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                record_type TEXT DEFAULT 'override',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fulfilled_at TIMESTAMP,
                UNIQUE(chapter, constraint_type, constraint_id)
            );
            CREATE TABLE IF NOT EXISTS chase_debt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_type TEXT NOT NULL,
                original_amount REAL DEFAULT 1.0,
                current_amount REAL DEFAULT 1.0,
                interest_rate REAL DEFAULT 0.1,
                source_chapter INTEGER NOT NULL,
                due_chapter INTEGER NOT NULL,
                override_contract_id INTEGER,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS debt_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                amount REAL NOT NULL,
                chapter INTEGER NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS chapter_reading_power (
                chapter INTEGER PRIMARY KEY,
                hook_type TEXT,
                hook_strength TEXT DEFAULT 'medium',
                coolpoint_patterns TEXT,
                micropayoffs TEXT,
                hard_violations TEXT,
                soft_suggestions TEXT,
                is_transition INTEGER DEFAULT 0,
                override_count INTEGER DEFAULT 0,
                debt_balance REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS invalid_facts (
                id INTEGER PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                marked_by TEXT NOT NULL,
                marked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP,
                chapter_discovered INTEGER
            );
            CREATE TABLE IF NOT EXISTS review_metrics (
                start_chapter INTEGER NOT NULL,
                end_chapter INTEGER NOT NULL,
                overall_score REAL DEFAULT 0,
                dimension_scores TEXT,
                severity_counts TEXT,
                critical_issues TEXT,
                report_file TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (start_chapter, end_chapter)
            );
            CREATE TABLE IF NOT EXISTS rag_query_log (
                id INTEGER PRIMARY KEY,
                query TEXT,
                query_type TEXT,
                results_count INTEGER,
                hit_sources TEXT,
                latency_ms INTEGER,
                chapter INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tool_call_stats (
                id INTEGER PRIMARY KEY,
                tool_name TEXT,
                success BOOLEAN,
                retry_count INTEGER DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                chapter INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS writing_checklist_scores (
                chapter INTEGER PRIMARY KEY,
                template TEXT DEFAULT 'plot',
                total_items INTEGER DEFAULT 0,
                required_items INTEGER DEFAULT 0,
                completed_items INTEGER DEFAULT 0,
                completed_required INTEGER DEFAULT 0,
                total_weight REAL DEFAULT 0.0,
                completed_weight REAL DEFAULT 0.0,
                completion_rate REAL DEFAULT 0.0,
                score REAL DEFAULT 0.0,
                score_breakdown TEXT,
                pending_items TEXT,
                source TEXT DEFAULT 'webnovel_studio',
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS story_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                chapter INTEGER,
                event_type TEXT,
                subject TEXT,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        try:
            cursor.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chapter_fts USING fts5(
                    chapter UNINDEXED,
                    title,
                    summary,
                    body
                );
                """
            )
        except sqlite3.Error:
            pass
        conn.commit()

    def _compute_total_words(self) -> int:
        body_dir = self.project_root / "正文"
        if not body_dir.is_dir() or body_dir.is_symlink():
            return 0
        total = 0
        for path in body_dir.glob("*.md"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += _count_prose_words(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
        return total

    def build_enriched_state(
        self,
        state: Mapping[str, Any],
        *,
        chapter: int,
        title: str,
        body: str,
        memory: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return an updated copy of `state` with rich entities, threads, strand, and chapter_meta."""
        updated = dict(state)
        lang = detect_language(title, body)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 1. Normalize project & project_info so both Dashboard (`project`) and Runtime (`project_info`) work
        project_info = dict(updated.get("project_info") or updated.get("project") or {})
        if project_info:
            project_info.setdefault("language", lang)
            updated["project_info"] = project_info
            project_block = dict(updated.get("project") or {})
            for k in ("title", "genre", "premise", "language"):
                if k in project_info and k not in project_block:
                    project_block[k] = project_info[k]
            updated["project"] = project_block

        # 2. Update progress
        progress = dict(updated.get("progress") or {})
        cur_ch = progress.get("current_chapter", 0)
        if isinstance(cur_ch, bool) or not isinstance(cur_ch, int):
            cur_ch = 0
        progress["current_chapter"] = max(cur_ch, chapter)
        progress["total_words"] = max(
            _count_prose_words(body),
            self._compute_total_words(),
        )
        progress["last_updated"] = today
        updated["progress"] = progress

        # 3. Extract and merge entities_v3
        entities_v3 = dict(updated.get("entities_v3") or {})
        role_map = dict(entities_v3.get("角色") or {})
        loc_map = dict(entities_v3.get("地点") or {})

        # Seed from 设定集 if empty
        if not role_map:
            for item in collect_codex_entities(self.project_root, updated, body + "\n" + title, chapter):
                if item.get("type") == "角色":
                    eid = str(item["id"])
                    role_map[eid] = {
                        "name": item["name"],
                        "canonical_name": item["canonical_name"],
                        "tier": item.get("tier") or ("核心" if item.get("is_protagonist") else "重要"),
                        "desc": item.get("desc") or "",
                        "aliases": list(item.get("aliases") or []),
                        "is_protagonist": bool(item.get("is_protagonist", False)),
                        "first_appearance": int(item.get("first_appearance") or chapter),
                        "last_appearance": chapter,
                        "current": dict(item.get("current") or {}),
                    }

        # Apply character_changes and explicit entities from memory
        mem = memory or {}
        char_changes = [
            str(c).strip() for c in (mem.get("character_changes") or []) if str(c).strip()
        ]
        explicit_chars = [
            str(c).strip() for c in (mem.get("characters") or []) if str(c).strip()
        ]
        location = str(mem.get("location") or "").strip()

        for change in char_changes:
            for eid, info in role_map.items():
                cname = str(info.get("canonical_name") or info.get("name") or "")
                if cname and cname in change:
                    info["last_appearance"] = max(int(info.get("last_appearance") or chapter), chapter)
                    cur = dict(info.get("current") or {})
                    cur["latest_action"] = change[:140]
                    if location:
                        cur["location"] = location
                    info["current"] = cur

        for cname in explicit_chars:
            if not cname or cname.lower() in _STOP_ENTITY_NAMES or len(cname) > 20:
                continue
            existing_id = next(
                (
                    eid
                    for eid, info in role_map.items()
                    if info.get("canonical_name") == cname or info.get("name") == cname
                ),
                None,
            )
            if existing_id is None:
                eid = _slug_id(cname, "char")
                role_map[eid] = {
                    "name": cname,
                    "canonical_name": cname,
                    "tier": "核心" if not role_map else "重要",
                    "desc": "",
                    "aliases": [],
                    "is_protagonist": not bool(role_map),
                    "first_appearance": chapter,
                    "last_appearance": chapter,
                    "current": {"location": location} if location else {},
                }
            else:
                role_map[existing_id]["last_appearance"] = max(
                    int(role_map[existing_id].get("last_appearance") or chapter),
                    chapter,
                )

        # Also check which known characters appear in `body`
        for eid, info in role_map.items():
            cname = str(info.get("canonical_name") or info.get("name") or "")
            if cname and cname in body:
                info["last_appearance"] = max(int(info.get("last_appearance") or chapter), chapter)

        if role_map:
            entities_v3["角色"] = role_map
        if location:
            lid = _slug_id(location, "loc")
            loc_map[lid] = {
                "name": location,
                "canonical_name": location,
                "tier": "重要",
                "first_appearance": int((loc_map.get(lid) or {}).get("first_appearance") or chapter),
                "last_appearance": chapter,
                "current": {"chapter": chapter},
            }
            entities_v3["地点"] = loc_map
        if entities_v3:
            updated["entities_v3"] = entities_v3

        # 4. Update protagonist_state
        protag_entry = next(
            (info for info in role_map.values() if info.get("is_protagonist")),
            next(iter(role_map.values()), None),
        )
        if isinstance(protag_entry, Mapping):
            protag_state = dict(updated.get("protagonist_state") or {})
            protag_state["name"] = str(protag_entry.get("canonical_name") or protag_entry.get("name") or "")
            cur_info = protag_entry.get("current") if isinstance(protag_entry.get("current"), Mapping) else {}
            if location or cur_info.get("location"):
                protag_state["location"] = str(location or cur_info.get("location") or "")
            if cur_info.get("latest_action"):
                protag_state["latest_action"] = str(cur_info["latest_action"])
            updated["protagonist_state"] = protag_state

        # 5. Update plot_threads.foreshadowing
        plot_threads = dict(updated.get("plot_threads") or {})
        foreshadowing: list[dict[str, Any]] = [
            dict(item)
            for item in (plot_threads.get("foreshadowing") or [])
            if isinstance(item, Mapping)
        ]
        resolved_inputs = {
            str(r).strip() for r in (mem.get("resolved_threads") or []) if str(r).strip()
        }
        for item in foreshadowing:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if item.get("status") != "resolved":
                if content in resolved_inputs or any(
                    r in content or content in r for r in resolved_inputs
                ):
                    item["status"] = "resolved"
                    item["resolved_chapter"] = chapter
                    item["urgency"] = 0
                else:
                    planted = int(item.get("planted_chapter") or 1)
                    elapsed = max(0, chapter - planted)
                    item["urgency"] = min(100, max(int(item.get("urgency") or 30), 30 + elapsed * 10))

        existing_contents = {str(item.get("content") or "").strip() for item in foreshadowing}
        for thread in mem.get("open_threads") or []:
            t_str = str(thread).strip()
            if not t_str or t_str in existing_contents:
                continue
            existing_contents.add(t_str)
            foreshadowing.append(
                {
                    "content": t_str,
                    "planted_chapter": chapter,
                    "target_chapter": chapter + 8,
                    "resolved_chapter": None,
                    "status": "active",
                    "urgency": 40,
                }
            )
        plot_threads["foreshadowing"] = foreshadowing
        updated["plot_threads"] = plot_threads

        # 6. Update strand_tracker & chapter_meta
        hook_info = classify_chapter_hook(body, language=lang)
        strand = str(mem.get("strand") or "").strip().lower()
        if strand not in {"quest", "fire", "constellation"}:
            if hook_info["tension_score"] >= 8:
                strand = "quest"
            elif char_changes and len(role_map) >= 2:
                strand = "fire"
            else:
                strand = "constellation" if chapter % 3 == 0 else "quest"

        strand_tracker = dict(updated.get("strand_tracker") or {})
        history: list[dict[str, Any]] = [
            dict(item)
            for item in (strand_tracker.get("history") or [])
            if isinstance(item, Mapping) and int(item.get("chapter") or 0) != chapter
        ]
        history.append({"chapter": chapter, "strand": strand})
        history.sort(key=lambda item: int(item.get("chapter") or 0))
        strand_tracker["history"] = history
        updated["strand_tracker"] = strand_tracker

        chapter_meta = dict(updated.get("chapter_meta") or {})
        ending_text = str(mem.get("ending") or hook_info.get("ending_snippet") or "").strip()
        chapter_meta[f"{chapter:04d}"] = {
            "title": title,
            "hook": {
                "type": str(mem.get("hook_type") or hook_info["hook_type"]),
                "content": ending_text[:160],
                "strength": str(mem.get("hook_strength") or hook_info["hook_strength"]),
            },
            "pattern": {
                "opening": "Scene Entry" if lang == "en" else "场景切入",
                "hook": str(mem.get("hook_type") or hook_info["hook_type"]),
                "emotion_rhythm": "Escalating" if lang == "en" else "蓄势→爆发",
                "info_density": "medium",
            },
            "ending": {
                "location": location or str((updated.get("protagonist_state") or {}).get("location") or ""),
                "emotion": "High Tension" if hook_info["hook_strength"] == "strong" else "Suspense",
                "tension_score": int(mem.get("tension_score") or hook_info["tension_score"]),
            },
        }
        updated["chapter_meta"] = chapter_meta
        return updated

    def sync_after_chapter(
        self,
        *,
        chapter: int,
        title: str,
        body: str,
        state: Mapping[str, Any] | None = None,
        memory: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist chapter metadata, entities, relationships, reading power, and FTS5 index to `index.db`."""
        if not self.webnovel_dir.is_dir() or self.webnovel_dir.is_symlink():
            return
        if self.db_path.is_symlink():
            return

        current_state = dict(state or {})
        if not current_state and self.state_path.is_file() and not self.state_path.is_symlink():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current_state = loaded
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
                current_state = {}

        mem = dict(memory or {})
        lang = detect_language(title, body)
        lint = lint_prose(body, language=lang)
        hook = lint["hook"]
        word_count = _count_prose_words(body)
        summary = str(mem.get("summary") or "").strip()
        if not summary:
            paragraphs = [
                p.strip()
                for p in body.splitlines()
                if p.strip() and not p.strip().startswith("#")
            ]
            summary = (paragraphs[0][:160] if paragraphs else title).strip()

        entities_v3 = current_state.get("entities_v3") if isinstance(current_state, Mapping) else {}
        role_map = (
            entities_v3.get("角色")
            if isinstance(entities_v3, Mapping) and isinstance(entities_v3.get("角色"), Mapping)
            else {}
        )
        appearing_chars: list[str] = []
        for _eid, info in role_map.items():
            if not isinstance(info, Mapping):
                continue
            cname = str(info.get("canonical_name") or info.get("name") or "").strip()
            if cname and (cname in body or int(info.get("last_appearance") or 0) == chapter):
                appearing_chars.append(cname)
        if not appearing_chars:
            for c in mem.get("characters") or []:
                if str(c).strip():
                    appearing_chars.append(str(c).strip())

        location = str(
            mem.get("location")
            or ((current_state.get("protagonist_state") or {}).get("location") if isinstance(current_state.get("protagonist_state"), Mapping) else "")
            or ""
        ).strip()

        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self._init_db_schema(conn)
            cursor = conn.cursor()

            # 1. Upsert chapter row
            cursor.execute(
                """
                INSERT INTO chapters (chapter, title, location, word_count, characters, summary)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chapter) DO UPDATE SET
                    title = excluded.title,
                    location = excluded.location,
                    word_count = excluded.word_count,
                    characters = excluded.characters,
                    summary = excluded.summary
                """,
                (
                    chapter,
                    title,
                    location,
                    word_count,
                    json.dumps(appearing_chars, ensure_ascii=False),
                    summary,
                ),
            )

            # 2. Upsert scenes from paragraphs
            lines = body.splitlines()
            non_empty = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
            chunk_size = max(1, (len(non_empty) + 2) // 3)
            cursor.execute("DELETE FROM scenes WHERE chapter = ?", (chapter,))
            for idx in range(min(3, max(1, len(non_empty)))):
                start_i = idx * chunk_size
                end_i = min(len(non_empty), (idx + 1) * chunk_size)
                segment = non_empty[start_i:end_i]
                if not segment:
                    continue
                scene_summary = segment[0][:140]
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO scenes
                    (chapter, scene_index, start_line, end_line, location, summary, characters)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chapter,
                        idx + 1,
                        start_i + 1,
                        end_i,
                        location,
                        scene_summary,
                        json.dumps(appearing_chars, ensure_ascii=False),
                    ),
                )

            # 3. Upsert entities & aliases from entities_v3
            if isinstance(entities_v3, Mapping):
                for etype, group in entities_v3.items():
                    if not isinstance(group, Mapping):
                        continue
                    for eid, info in group.items():
                        if not isinstance(info, Mapping):
                            continue
                        cname = str(info.get("canonical_name") or info.get("name") or eid).strip()
                        if not cname:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO entities
                            (id, type, canonical_name, tier, desc, current_json, first_appearance, last_appearance, is_protagonist, is_archived, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                            ON CONFLICT(id) DO UPDATE SET
                                canonical_name = excluded.canonical_name,
                                tier = excluded.tier,
                                desc = CASE WHEN excluded.desc != '' THEN excluded.desc ELSE entities.desc END,
                                current_json = excluded.current_json,
                                last_appearance = MAX(entities.last_appearance, excluded.last_appearance),
                                is_protagonist = MAX(entities.is_protagonist, excluded.is_protagonist),
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (
                                str(eid),
                                str(etype),
                                cname,
                                str(info.get("tier") or "重要"),
                                str(info.get("desc") or ""),
                                json.dumps(info.get("current") or {}, ensure_ascii=False),
                                int(info.get("first_appearance") or chapter),
                                int(info.get("last_appearance") or chapter),
                                1 if info.get("is_protagonist") else 0,
                            ),
                        )
                        cursor.execute(
                            "INSERT OR IGNORE INTO aliases (alias, entity_id, entity_type) VALUES (?, ?, ?)",
                            (cname, str(eid), str(etype)),
                        )
                        for alias in info.get("aliases") or []:
                            alias_str = str(alias).strip()
                            if alias_str:
                                cursor.execute(
                                    "INSERT OR IGNORE INTO aliases (alias, entity_id, entity_type) VALUES (?, ?, ?)",
                                    (alias_str, str(eid), str(etype)),
                                )

            # 4. Record state_changes from character_changes
            cursor.execute("DELETE FROM state_changes WHERE chapter = ?", (chapter,))
            for change in mem.get("character_changes") or []:
                change_str = str(change).strip()
                if not change_str:
                    continue
                matched_id = appearing_chars[0] if appearing_chars else "protagonist"
                for eid, info in role_map.items():
                    cname = str(info.get("canonical_name") or info.get("name") or "")
                    if cname and cname in change_str:
                        matched_id = str(eid)
                        break
                cursor.execute(
                    """
                    INSERT INTO state_changes (entity_id, field, old_value, new_value, reason, chapter)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (matched_id, "progression", "", change_str[:160], title, chapter),
                )

            # 5. Upsert relationships between co-appearing characters
            if len(appearing_chars) >= 2:
                lead = appearing_chars[0]
                for other in appearing_chars[1:4]:
                    if other == lead:
                        continue
                    rel_desc = summary[:140]
                    cursor.execute(
                        """
                        INSERT INTO relationships (from_entity, to_entity, type, description, chapter)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(from_entity, to_entity, type) DO UPDATE SET
                            description = excluded.description,
                            chapter = excluded.chapter
                        """,
                        (lead, other, "同行/交锋" if lang == "zh" else "Active Link", rel_desc, chapter),
                    )
                    cursor.execute(
                        """
                        INSERT INTO relationship_events
                        (from_entity, to_entity, type, action, polarity, strength, description, chapter, scene_index, evidence, confidence)
                        VALUES (?, ?, ?, 'update', 0, 0.7, ?, ?, 1, ?, 0.9)
                        """,
                        (lead, other, "同行/交锋" if lang == "zh" else "Active Link", rel_desc, chapter, title),
                    )

            # 6. Upsert chapter_reading_power & writing_checklist_scores
            active_debts = [
                item
                for item in ((current_state.get("plot_threads") or {}).get("foreshadowing") or [])
                if isinstance(item, Mapping) and item.get("status") != "resolved"
            ]
            cursor.execute(
                """
                INSERT INTO chapter_reading_power
                (chapter, hook_type, hook_strength, coolpoint_patterns, micropayoffs, hard_violations, soft_suggestions, is_transition, override_count, debt_balance, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chapter) DO UPDATE SET
                    hook_type = excluded.hook_type,
                    hook_strength = excluded.hook_strength,
                    coolpoint_patterns = excluded.coolpoint_patterns,
                    micropayoffs = excluded.micropayoffs,
                    soft_suggestions = excluded.soft_suggestions,
                    debt_balance = excluded.debt_balance,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chapter,
                    str(mem.get("hook_type") or hook["hook_type"]),
                    str(mem.get("hook_strength") or hook["hook_strength"]),
                    json.dumps([hook["hook_type"]], ensure_ascii=False),
                    json.dumps(mem.get("character_changes") or [], ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    json.dumps(lint["edit_directives"], ensure_ascii=False),
                    float(len(active_debts)),
                ),
            )
            cursor.execute(
                """
                INSERT INTO writing_checklist_scores
                (chapter, template, total_items, required_items, completed_items, completed_required, total_weight, completed_weight, completion_rate, score, score_breakdown, pending_items, source, notes, updated_at)
                VALUES (?, 'plot', 4, 4, 4, 4, 100.0, ?, 1.0, ?, ?, '[]', 'webnovel_studio', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chapter) DO UPDATE SET
                    completed_weight = excluded.completed_weight,
                    score = excluded.score,
                    score_breakdown = excluded.score_breakdown,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chapter,
                    float(lint["freshness_score"]),
                    float(lint["freshness_score"]),
                    json.dumps(
                        {
                            "freshness_score": lint["freshness_score"],
                            "cliche_count": lint["cliche_count"],
                            "dialogue_ratio": lint["dialogue_ratio"],
                            "tension_score": hook["tension_score"],
                        },
                        ensure_ascii=False,
                    ),
                    title,
                ),
            )

            # 7. Sync FTS5 table if available
            try:
                cursor.execute("DELETE FROM chapter_fts WHERE chapter = ?", (str(chapter),))
                cursor.execute(
                    "INSERT INTO chapter_fts (chapter, title, summary, body) VALUES (?, ?, ?, ?)",
                    (str(chapter), title, summary, body),
                )
            except sqlite3.Error:
                pass

            conn.commit()

    def sync_after_review(
        self,
        *,
        start_chapter: int,
        end_chapter: int,
        overall_score: float,
        dimension_scores: Mapping[str, float],
        severity_counts: Mapping[str, int],
        critical_issues: Sequence[str],
        report_file: str,
    ) -> None:
        """Record quality review metrics into `.webnovel/index.db` for Dashboard visualization."""
        if not self.webnovel_dir.is_dir() or self.webnovel_dir.is_symlink():
            return
        if self.db_path.is_symlink():
            return
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self._init_db_schema(conn)
            conn.execute(
                """
                INSERT INTO review_metrics
                (start_chapter, end_chapter, overall_score, dimension_scores, severity_counts, critical_issues, report_file, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'Reviewed by NovelQualityService', CURRENT_TIMESTAMP)
                ON CONFLICT(start_chapter, end_chapter) DO UPDATE SET
                    overall_score = excluded.overall_score,
                    dimension_scores = excluded.dimension_scores,
                    severity_counts = excluded.severity_counts,
                    critical_issues = excluded.critical_issues,
                    report_file = excluded.report_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    start_chapter,
                    end_chapter,
                    float(overall_score),
                    json.dumps(dict(dimension_scores), ensure_ascii=False),
                    json.dumps(dict(severity_counts), ensure_ascii=False),
                    json.dumps(list(critical_issues), ensure_ascii=False),
                    report_file,
                ),
            )
            conn.commit()

    def ensure_dashboard_db(self) -> None:
        """Ensure `.webnovel/index.db` exists and reflects any existing state/chapters before Dashboard launch."""
        if not self.webnovel_dir.is_dir() or self.webnovel_dir.is_symlink():
            return
        if self.db_path.is_symlink():
            return
        state: dict[str, Any] = {}
        if self.state_path.is_file() and not self.state_path.is_symlink():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
                state = {}

        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self._init_db_schema(conn)
            cursor = conn.cursor()
            entities_v3 = state.get("entities_v3") if isinstance(state, Mapping) else {}
            if not isinstance(entities_v3, Mapping) or not entities_v3:
                extracted = collect_codex_entities(self.project_root, state, "", 1)
                role_group: dict[str, Any] = {}
                for item in extracted:
                    role_group[str(item["id"])] = item
                if role_group:
                    entities_v3 = {"角色": role_group}
            if isinstance(entities_v3, Mapping):
                for etype, group in entities_v3.items():
                    if not isinstance(group, Mapping):
                        continue
                    for eid, info in group.items():
                        if not isinstance(info, Mapping):
                            continue
                        cname = str(info.get("canonical_name") or info.get("name") or eid).strip()
                        if not cname:
                            continue
                        cursor.execute(
                            """
                            INSERT OR IGNORE INTO entities
                            (id, type, canonical_name, tier, desc, current_json, first_appearance, last_appearance, is_protagonist, is_archived)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """,
                            (
                                str(eid),
                                str(etype),
                                cname,
                                str(info.get("tier") or "重要"),
                                str(info.get("desc") or ""),
                                json.dumps(info.get("current") or {}, ensure_ascii=False),
                                int(info.get("first_appearance") or 1),
                                int(info.get("last_appearance") or 1),
                                1 if info.get("is_protagonist") else 0,
                            ),
                        )
            conn.commit()

        body_dir = self.project_root / "正文"
        memory_map: dict[str, Any] = {}
        memory_path = self.webnovel_dir / "novel_memory.json"
        if memory_path.is_file() and not memory_path.is_symlink():
            try:
                mem_json = json.loads(memory_path.read_text(encoding="utf-8"))
                if isinstance(mem_json, Mapping) and isinstance(mem_json.get("chapters"), Mapping):
                    memory_map = dict(mem_json["chapters"])
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
                pass

        if body_dir.is_dir() and not body_dir.is_symlink():
            for path in sorted(body_dir.glob("*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                match = _CHAPTER_FILE_RE.match(path.name)
                if not match:
                    continue
                ch_num = int(match.group(1) or match.group(2) or 0)
                if ch_num <= 0:
                    continue
                ch_title = (match.group(3) or f"第{ch_num}章").strip()
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                ch_mem = memory_map.get(str(ch_num)) if isinstance(memory_map.get(str(ch_num)), Mapping) else None
                state = self.build_enriched_state(
                    state,
                    chapter=ch_num,
                    title=ch_title,
                    body=text,
                    memory=ch_mem,
                )
                self.sync_after_chapter(
                    chapter=ch_num,
                    title=ch_title,
                    body=text,
                    state=state,
                    memory=ch_mem,
                )


__all__ = [
    "StoryStateSync",
    "collect_codex_entities",
    "collect_foreshadowing_alerts",
    "retrieve_local_passages",
]

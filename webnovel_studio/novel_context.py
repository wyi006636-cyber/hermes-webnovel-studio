"""Human-readable context assembly for novel writing.

The writing path deliberately treats outlines, settings, prior prose, and the
writer's current instruction as its sources. Story System contracts, indexes,
RAG, vectors, review artifacts, and projection state are not prerequisites.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .de_ai_linter import build_scene_beats, detect_language
from .story_state_sync import (
    collect_codex_entities,
    collect_foreshadowing_alerts,
    retrieve_local_passages,
)


class NovelContextError(RuntimeError):
    """The project cannot safely provide writing context."""


@dataclass(frozen=True)
class NovelContext:
    book_title: str
    genre: str
    premise: str
    chapter: int
    title: str
    chapter_outline: str
    book_context: str
    setting_context: str
    recent_memory: str
    previous_ending: str
    author_instruction: str
    warnings: tuple[str, ...]
    language: str = "zh"
    active_entities: tuple[dict[str, Any], ...] = ()
    foreshadowing_alerts: tuple[dict[str, Any], ...] = ()
    retrieved_passages: tuple[dict[str, Any], ...] = ()
    scene_beats: tuple[dict[str, Any], ...] = ()

    def as_prompt(self) -> dict[str, Any]:
        value = asdict(self)
        value["authority_order"] = [
            "setting_context",
            "active_entities",
            "author_instruction",
            "chapter_outline",
            "scene_beats",
            "foreshadowing_alerts",
            "book_context",
            "recent_memory",
            "retrieved_passages",
            "previous_ending",
        ]
        value["authority_rule"] = (
            "发生冲突时，以作者写入设定集的已确认设定为最高事实；当前指令只能决定本章写法，"
            "不得暗中改写已确认设定。章纲和总纲高于自动提取的轻量记忆，前文原文高于记忆摘要。"
            " When conflicts arise, confirmed settings in 设定集 take highest priority."
        )
        return value


class NovelContextBuilder:
    """Build one chapter's useful writing context without contract gates."""

    _CHAPTER_HEADING = re.compile(
        r"^\s*#{1,6}\s*(?:第\s*0*(\d+)\s*章|Chapter\s*0*(\d+))(?:\s*[-—:：]?\s*(.*?))?\s*$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        project_root: str | Path,
        *,
        outline_limit: int = 7000,
        setting_limit: int = 6000,
        memory_limit: int = 4000,
        previous_ending_limit: int = 1200,
    ) -> None:
        root = Path(project_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        if any(component.is_symlink() for component in (root, *root.parents)):
            raise NovelContextError("novel project path must not contain a symlink")
        try:
            self.project_root = root.resolve(strict=True)
        except OSError as exc:
            raise NovelContextError("novel project root does not exist") from exc
        if not self.project_root.is_dir() or self.project_root.is_symlink():
            raise NovelContextError("novel project root must be a real directory")
        self.outline_limit = outline_limit
        self.setting_limit = setting_limit
        self.memory_limit = memory_limit
        self.previous_ending_limit = previous_ending_limit

    def _safe_text(self, path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            return ""
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.project_root)
            return resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return ""

    def _state(self, warnings: list[str]) -> dict[str, Any]:
        state_path = self.project_root / ".webnovel" / "state.json"
        if state_path.is_symlink():
            warnings.append("state_unsafe")
            return {}
        if not state_path.is_file():
            warnings.append("state_missing")
            return {}
        try:
            value = json.loads(self._safe_text(state_path))
        except (TypeError, ValueError, json.JSONDecodeError):
            warnings.append("state_invalid")
            return {}
        if not isinstance(value, dict):
            warnings.append("state_invalid")
            return {}
        return value

    @staticmethod
    def _project_info(state: dict[str, Any]) -> dict[str, Any]:
        for key in ("project_info", "project"):
            value = state.get(key)
            if isinstance(value, dict):
                return value
        return {}

    def _markdown_files(self, directory_name: str) -> list[Path]:
        directory = self.project_root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            return []
        return sorted(
            (
                path
                for path in directory.rglob("*.md")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (
                0 if "详细大纲" in path.name else 1 if "节拍" in path.name else 2,
                path.as_posix(),
            ),
        )

    def _chapter_section(self, chapter: int) -> tuple[str, str]:
        for path in self._markdown_files("大纲"):
            lines = self._safe_text(path).splitlines()
            start: int | None = None
            title = ""
            for index, line in enumerate(lines):
                match = self._CHAPTER_HEADING.match(line)
                if not match:
                    continue
                matched_number = int(match.group(1) or match.group(2) or 0)
                if matched_number != chapter:
                    continue
                start = index
                title = (match.group(3) or "").strip(" -—:：")
                break
            if start is None:
                continue
            end = len(lines)
            for index in range(start + 1, len(lines)):
                if self._CHAPTER_HEADING.match(lines[index]):
                    end = index
                    break
            section = "\n".join(lines[start:end]).strip()
            if section:
                return title, section[: self.outline_limit]
        return "", ""

    def _book_context(self, chapter_outline: str) -> str:
        chunks: list[str] = []
        remaining = self.outline_limit
        for path in self._markdown_files("大纲"):
            if "总纲" not in path.name and "节拍" not in path.name and "卷纲" not in path.name:
                continue
            text = self._safe_text(path).strip()
            if not text or text == chapter_outline:
                continue
            chunk = f"[{path.name}]\n{text}"
            separator = 2 if chunks else 0
            available = remaining - separator
            if available <= 0:
                break
            if len(chunk) > available:
                chunk = chunk[:available]
            chunks.append(chunk)
            remaining -= separator + len(chunk)
            if remaining <= 0:
                break
        return "\n\n".join(chunks)

    @staticmethod
    def _relevance_score(text: str, reference: str) -> int:
        compact = "".join(re.findall(r"[\u4e00-\u9fff]", reference))
        bigrams = {compact[index : index + 2] for index in range(len(compact) - 1)}
        words = {
            word.lower()
            for word in re.findall(r"[A-Za-z0-9_]{2,}", reference)
        }
        lowered = text.lower()
        return sum(term in text for term in bigrams) + sum(
            word in lowered for word in words
        )

    def _settings(self, relevance_text: str) -> str:
        records: list[tuple[int, Path, str]] = []
        for path in self._markdown_files("设定集"):
            text = self._safe_text(path).strip()
            if text:
                score = self._relevance_score(text, relevance_text)
                records.append((score, path, text))
        records.sort(key=lambda item: (-item[0], item[1].as_posix()))

        chunks: list[str] = []
        remaining = self.setting_limit
        first_pass_cap = max(80, self.setting_limit // max(1, min(len(records), 4)))
        for _score, path, text in records:
            chunk = f"[{path.name}]\n{text}"
            separator = 2 if chunks else 0
            available = remaining - separator
            if available <= 0:
                break
            allocation = min(available, first_pass_cap)
            if len(chunk) > allocation:
                chunk = chunk[:allocation]
            chunks.append(chunk)
            remaining -= separator + len(chunk)
            if remaining <= 0:
                break
        return "\n\n".join(chunks)

    def _memory(self, chapter: int, warnings: list[str]) -> str:
        path = self.project_root / ".webnovel" / "novel_memory.json"
        if not path.is_file() or path.is_symlink():
            warnings.append("memory_missing")
            return ""
        try:
            value = json.loads(self._safe_text(path))
            chapters = value.get("chapters") if isinstance(value, dict) else None
            if not isinstance(chapters, dict):
                raise ValueError("chapters missing")
            eligible: list[tuple[int, dict[str, Any]]] = []
            for raw_number, item in chapters.items():
                try:
                    number = int(raw_number)
                except (TypeError, ValueError):
                    continue
                if number < chapter and isinstance(item, dict):
                    eligible.append((number, item))
            selected = sorted(eligible, key=lambda pair: pair[0])[-2:]
            memory = "\n\n".join(
                f"[第{number}章记忆]\n"
                + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                for number, item in selected
            )
            return memory[-self.memory_limit :]
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            warnings.append("memory_invalid")
            return ""

    def _previous_ending(self, chapter: int, warnings: list[str]) -> str:
        if chapter <= 1:
            return ""
        body_dir = self.project_root / "正文"
        if body_dir.is_symlink() or not body_dir.is_dir():
            warnings.append("previous_chapter_missing")
            return ""
        candidates = sorted(body_dir.glob(f"第{chapter - 1:04d}章-*.md"))
        if not candidates:
            candidates = sorted(body_dir.glob(f"第{chapter - 1}章*.md"))
        if not candidates:
            candidates = sorted(body_dir.glob(f"Chapter-{chapter - 1:04d}*.md"))
        if not candidates:
            candidates = sorted(body_dir.glob(f"Chapter-{chapter - 1}*.md"))
        for path in candidates:
            text = self._safe_text(path).strip()
            if text:
                return text[-self.previous_ending_limit :]
        warnings.append("previous_chapter_missing")
        return ""

    def build(self, chapter: int, *, author_instruction: str = "") -> NovelContext:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise NovelContextError("chapter must be a positive integer")
        if not isinstance(author_instruction, str):
            raise NovelContextError("author instruction must be text")

        warnings: list[str] = []
        state = self._state(warnings)
        project = self._project_info(state)
        if not project:
            proposal_path = self.project_root / ".webnovel" / "creative_proposal.json"
            try:
                proposal = json.loads(self._safe_text(proposal_path))
            except (TypeError, ValueError, json.JSONDecodeError):
                proposal = {}
            if isinstance(proposal, dict):
                project = proposal
        title, chapter_outline = self._chapter_section(chapter)
        if not chapter_outline:
            warnings.append("outline_missing")
        lang = str(project.get("language") or "").strip().lower()
        if lang not in {"zh", "en"}:
            lang = detect_language(
                project.get("title"),
                project.get("genre"),
                project.get("premise"),
                title,
                chapter_outline,
                author_instruction,
            )
        if not title:
            title = f"Chapter {chapter}" if lang == "en" else f"第{chapter}章"
        relevance_source = "\n".join((chapter_outline, author_instruction, title))
        settings = self._settings(relevance_source)
        if not settings:
            warnings.append("settings_missing")
        previous_ending = self._previous_ending(chapter, warnings)
        active_entities = tuple(
            collect_codex_entities(
                self.project_root,
                state,
                relevance_source + "\n" + previous_ending,
                chapter,
            )
        )
        foreshadowing_alerts = tuple(
            collect_foreshadowing_alerts(self.project_root, state, chapter)
        )
        retrieved_passages = tuple(
            retrieve_local_passages(
                self.project_root,
                relevance_source,
                before_chapter=chapter,
                limit=3,
            )
        )
        scene_beats = tuple(
            build_scene_beats(
                chapter=chapter,
                title=title,
                chapter_outline=chapter_outline,
                author_instruction=author_instruction,
                active_entities=active_entities,
                foreshadowing_alerts=foreshadowing_alerts,
                language=lang,
            )
        )

        default_book_title = "Untitled Novel" if lang == "en" else "未命名小说"
        return NovelContext(
            book_title=str(project.get("title") or state.get("title") or default_book_title),
            genre=str(project.get("genre_label") or project.get("genre") or ""),
            premise=str(project.get("premise") or state.get("premise") or ""),
            chapter=chapter,
            title=title,
            chapter_outline=chapter_outline,
            book_context=self._book_context(chapter_outline),
            setting_context=settings,
            recent_memory=self._memory(chapter, warnings),
            previous_ending=previous_ending,
            author_instruction=author_instruction.strip(),
            warnings=tuple(dict.fromkeys(warnings)),
            language=lang,
            active_entities=active_entities,
            foreshadowing_alerts=foreshadowing_alerts,
            retrieved_passages=retrieved_passages,
            scene_beats=scene_beats,
        )


__all__ = ["NovelContext", "NovelContextBuilder", "NovelContextError"]

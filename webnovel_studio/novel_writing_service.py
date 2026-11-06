"""Novel-first chapter writing.

A successful prose file is the primary result. Editing and lightweight memory
maintenance improve future chapters but never revoke already generated prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .de_ai_linter import lint_prose
from .novel_context import NovelContext, NovelContextBuilder
from .project_lock import ProjectLock
from .roles import load_role_prompt
from .story_state_sync import StoryStateSync


class NovelWritingError(RuntimeError):
    """The chapter could not produce usable prose."""


class ChapterAlreadyExistsError(NovelWritingError):
    """An existing author manuscript is protected from implicit overwrite."""


class UnsafeChapterPathError(NovelWritingError):
    """A chapter path would escape the manuscript directory."""


_MEMORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapter": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "character_changes": {"type": "array", "items": {"type": "string"}},
        "open_threads": {"type": "array", "items": {"type": "string"}},
        "resolved_threads": {"type": "array", "items": {"type": "string"}},
        "characters": {"type": "array", "items": {"type": "string"}},
        "location": {"type": "string"},
        "strand": {"type": "string"},
        "hook_type": {"type": "string"},
        "hook_strength": {"type": "string"},
        "tension_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "ending": {"type": "string"},
    },
    "required": [
        "chapter",
        "title",
        "summary",
        "character_changes",
        "open_threads",
        "ending",
    ],
    "additionalProperties": True,
}


class NovelWritingService:
    """Write, lightly edit, save, and remember one novel chapter."""

    QUALITY_RUBRIC = (
        "只检查并提升小说本身：剧情必须由人物选择真实推进，因果清楚；"
        "对白要符合人物身份、关系和当下情绪，并承担行动与潜台词；"
        "节奏要有松紧，场景要有明确进入点、变化和退出点；"
        "情绪要通过动作、感官和细节逐层积累；人物声音与关系变化必须可信；"
        "叙述视角稳定，句式、意象和文学表达服务故事而不炫技；"
        "章末要产生自然、具体的下一章阅读欲望。"
    )

    _HEADING = re.compile(
        r"^\s*#{1,6}\s*(?:第\s*0*\d+\s*章|Chapter\s*0*\d+)[^\n]*\n+",
        re.MULTILINE | re.IGNORECASE,
    )
    _TITLE_LINE = re.compile(
        r"^\s*(?:章名|标题|Chapter\s*Title|Title)\s*[:：][^\n]*\n+",
        re.MULTILINE | re.IGNORECASE,
    )
    _PREFIX = re.compile(
        r"^\s*(?:以下(?:是|为)(?:本章|章节)?正文|正文如下|这是(?:本章|章节)正文)\s*[:：]?\s*",
        re.IGNORECASE,
    )

    def __init__(
        self,
        project_root: str | Path,
        *,
        router: Any,
        context_builder: NovelContextBuilder | None = None,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        requested = Path(project_root).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        if any(component.is_symlink() for component in (requested, *requested.parents)):
            raise UnsafeChapterPathError("novel project path must not contain a symlink")
        self.project_root = requested.resolve(strict=True)
        self.router = router
        self.context_builder = context_builder or NovelContextBuilder(self.project_root)
        self.lock_timeout_seconds = lock_timeout_seconds
        self._safe_directory(Path(".webnovel"), label="project metadata")

    def _safe_directory(self, relative: Path, *, label: str) -> Path:
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeChapterPathError(f"{label} directory must not be a symlink")
            if current.exists() and not current.is_dir():
                raise UnsafeChapterPathError(f"{label} path must be a directory")
            current.mkdir(exist_ok=True)
        try:
            current.resolve(strict=True).relative_to(self.project_root)
        except (OSError, ValueError) as exc:
            raise UnsafeChapterPathError(
                f"{label} directory escapes the novel project"
            ) from exc
        return current

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            NovelWritingService._fsync_directory(path.parent)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        NovelWritingService._atomic_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _safe_title(value: str, chapter: int) -> str:
        title = re.sub(r"[\\/:*?\"<>|\x00]", "", value).strip(" .-—:：")
        title = re.sub(r"\s+", " ", title)
        return (title[:60] or f"第{chapter}章").strip()

    def _chapter_candidates(self, chapter: int) -> list[Path]:
        body_dir = self._safe_directory(Path("正文"), label="manuscript")
        candidates = {
            *body_dir.glob(f"第{chapter:04d}章*.md"),
            *body_dir.glob(f"第{chapter}章*.md"),
        }
        return sorted(candidates)

    def _chapter_path(self, chapter: int, title: str) -> Path:
        body_dir = (self.project_root / "正文").resolve(strict=True)
        path = body_dir / f"第{chapter:04d}章-{self._safe_title(title, chapter)}.md"
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(body_dir)
        except ValueError as exc:
            raise UnsafeChapterPathError("chapter file escapes the manuscript directory") from exc
        return resolved

    @staticmethod
    def _chapter_snapshot(paths: list[Path]) -> dict[Path, bytes]:
        snapshot: dict[Path, bytes] = {}
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise UnsafeChapterPathError("existing chapter path is unsafe")
            snapshot[path] = path.read_bytes()
        return snapshot

    @classmethod
    def _clean_body(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise NovelWritingError("Writer did not return text")
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        text = cls._HEADING.sub("", text, count=1)
        text = cls._TITLE_LINE.sub("", text, count=1)
        text = cls._PREFIX.sub("", text, count=1).strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                raise NovelWritingError("Writer returned data instead of novel prose")
        if not text or not any(character.isalnum() for character in text):
            raise NovelWritingError("Writer returned empty prose")
        return text

    @staticmethod
    def _messages(mode: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": load_role_prompt("writer")},
            {
                "role": "user",
                "content": json.dumps(
                    {"mode": mode, **payload},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]

    def _draft(
        self,
        context: NovelContext,
        *,
        target_chars: int | None,
        calls: list[dict[str, Any]],
    ) -> str:
        is_en = getattr(context, "language", "zh") == "en"
        payload: dict[str, Any] = {
            "story_context": context.as_prompt(),
            "scene_beats": list(getattr(context, "scene_beats", ())),
            "instruction": (
                "Write the chapter prose in English following scene_beats and active_entities. "
                "Output only novel prose—no chapter heading, outline, commentary, or system fields. "
                "Prioritize character agency, concrete sensory details, and dramatic escalation."
                if is_en
                else "直接按 scene_beats（场景节拍）与 active_entities（出场人物状态）写本章小说正文，"
                "不写标题、解释、提纲、复盘或任何系统字段。"
                "优先完成场景中的人物行动、冲突变化与情绪推进。"
            ),
        }
        if target_chars is not None:
            payload["soft_length_preference"] = (
                f"Target roughly {target_chars} characters/words as a soft preference; prioritize complete scenes without padding or abrupt truncation."
                if is_en
                else f"作者希望正文大约 {target_chars} 个中文字符；这是软方向，完整场景优先，"
                "不得为了凑数注水，也不得为了贴合长度截断剧情。"
            )
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                result = self.router.complete(
                    "writer",
                    self._messages("draft", payload),
                    purpose="webnovel:novel:draft",
                )
                body = self._clean_body(getattr(result, "text", None))
                calls.append(
                    {
                        "stage": "draft",
                        "attempt": attempt,
                        "continuity": {
                            "previous_ending": bool(context.previous_ending),
                            "recent_memory": bool(context.recent_memory),
                            "memory_character_changes": "character_changes"
                            in context.recent_memory,
                            "memory_open_threads": "open_threads"
                            in context.recent_memory,
                            "memory_ending": '"ending"' in context.recent_memory,
                        },
                    }
                )
                return body
            except Exception as exc:
                calls.append({"stage": "draft", "attempt": attempt, "failed": True})
                last_error = exc
                if attempt == 1:
                    payload["technical_retry"] = "上次没有返回可用小说正文。这次只返回正文。"
        raise NovelWritingError("Writer failed to produce usable novel prose") from last_error

    def _edit(
        self,
        context: NovelContext,
        draft: str,
        *,
        calls: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        is_en = getattr(context, "language", "zh") == "en"
        draft_lint = lint_prose(draft, language=getattr(context, "language", "zh"))
        try:
            result = self.router.complete(
                "writer",
                self._messages(
                    "literary_edit",
                    {
                        "story_context": context.as_prompt(),
                        "draft": draft,
                        "quality_rubric": self.QUALITY_RUBRIC,
                        "de_ai_lint": draft_lint,
                        "instruction": (
                            "Perform a single complete literary edit in English. Eliminate any flagged AI clichés "
                            "in de_ai_lint.edit_directives and return only the polished chapter prose."
                            if is_en
                            else "通读全文后只做一次完整小说编辑，重点消除 de_ai_lint.edit_directives 中指出的 AI 套话与重复句式。"
                            "返回编辑后的完整正文，不写标题、评价、修改说明或分数。不要为了格式、长度或内部系统要求改稿。"
                        ),
                    },
                ),
                purpose="webnovel:novel:edit",
            )
            calls.append({"stage": "edit", "attempt": 1})
            return self._clean_body(getattr(result, "text", None)), None
        except Exception:
            calls.append({"stage": "edit", "attempt": 1, "failed": True})
            return draft, "editor_failed"

    def _memory_payload(
        self,
        *,
        chapter: int,
        title: str,
        body: str,
        calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.router.complete_structured(
            "context_curator",
            instructions=(
                "提取供下一章续写与看板更新使用的故事记忆：本章实际发生了什么（summary）、"
                "人物状态如何变化（character_changes）、哪些伏笔/悬念未闭合（open_threads）、"
                "哪些旧伏笔已回收（resolved_threads，可选）、本章出场人物（characters，可选）、"
                "主要地点（location，可选）以及结尾停在哪里（ending）。不要评价文笔，不要输出系统字段。"
            ),
            input=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {"chapter": chapter, "title": title, "body": body},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ],
            json_schema=_MEMORY_SCHEMA,
            json_mode=True,
            schema_name="novel_memory_v1",
            system_prompt=(
                "你是小说连续性助理。只记录读下一章真正需要知道的故事事实，不做审查。"
            ),
            purpose="webnovel:novel:memory",
        )
        calls.append({"stage": "memory", "attempt": 1})
        candidate = getattr(result, "parsed", None)
        if not isinstance(candidate, dict):
            raise NovelWritingError("memory extractor returned invalid data")
        required = {"summary", "character_changes", "open_threads", "ending"}
        if not required.issubset(candidate):
            raise NovelWritingError("memory extractor omitted story memory")
        payload: dict[str, Any] = {
            "chapter": chapter,
            "title": title,
            "summary": str(candidate.get("summary") or "").strip(),
            "character_changes": [
                str(item).strip()
                for item in candidate.get("character_changes", [])
                if str(item).strip()
            ],
            "open_threads": [
                str(item).strip()
                for item in candidate.get("open_threads", [])
                if str(item).strip()
            ],
            "ending": str(candidate.get("ending") or "").strip(),
        }
        for opt_list in ("resolved_threads", "characters"):
            if isinstance(candidate.get(opt_list), list):
                cleaned = [
                    str(item).strip()
                    for item in candidate[opt_list]
                    if str(item).strip()
                ]
                if cleaned:
                    payload[opt_list] = cleaned
        for opt_str in ("location", "strand", "hook_type", "hook_strength"):
            val = str(candidate.get(opt_str) or "").strip()
            if val:
                payload[opt_str] = val
        if isinstance(candidate.get("tension_score"), int) and not isinstance(
            candidate.get("tension_score"), bool
        ):
            payload["tension_score"] = max(1, min(10, int(candidate["tension_score"])))
        return payload

    def _update_memory(self, chapter: int, payload: Mapping[str, Any]) -> None:
        path = self.project_root / ".webnovel" / "novel_memory.json"
        value: dict[str, Any] = {"schema_version": "novel-memory/v1", "chapters": {}}
        if path.is_file() and not path.is_symlink():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(current, dict) and isinstance(current.get("chapters"), dict):
                    value = current
            except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
                pass
        chapters = dict(value.get("chapters") or {})
        chapters[str(chapter)] = dict(payload)
        value["schema_version"] = "novel-memory/v1"
        value["chapters"] = chapters
        self._atomic_json(path, value)

    def _update_progress(
        self,
        chapter: int,
        *,
        expected_state: bytes | None,
        title: str = "",
        body: str = "",
        memory: Mapping[str, Any] | None = None,
    ) -> str | None:
        if expected_state is None:
            return None
        path = self.project_root / ".webnovel" / "state.json"
        try:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected_state:
                return "state_progress_update_skipped"
            state = json.loads(expected_state.decode("utf-8"))
            if not isinstance(state, dict):
                return None
            sync = StoryStateSync(self.project_root)
            enriched = sync.build_enriched_state(
                state,
                chapter=chapter,
                title=title or f"第{chapter}章",
                body=body,
                memory=memory,
            )
            self._atomic_json(path, enriched)
            try:
                sync.sync_after_chapter(
                    chapter=chapter,
                    title=title or f"第{chapter}章",
                    body=body,
                    state=enriched,
                    memory=memory,
                )
            except Exception:
                pass
            return None
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return "state_progress_update_failed"

    def _backup_existing(self, paths: list[Path]) -> None:
        if not paths:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self._safe_directory(
            Path(".webnovel/backups/chapters"), label="chapter backup"
        )
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise UnsafeChapterPathError("existing chapter path is unsafe")
            backup_path = destination / f"{timestamp}-{path.name}"
            suffix = 1
            while backup_path.exists() or backup_path.is_symlink():
                backup_path = destination / f"{timestamp}-{suffix}-{path.name}"
                suffix += 1
            shutil.copy2(path, backup_path)
            with backup_path.open("rb") as handle:
                os.fsync(handle.fileno())
        self._fsync_directory(destination)

    def write_chapter(
        self,
        chapter: int,
        *,
        author_instruction: str = "",
        target_chars: Any = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter < 1:
            raise NovelWritingError("chapter must be a positive integer")
        target_chars_warning = False
        if isinstance(target_chars, str) and target_chars.strip().isdigit():
            target_chars = int(target_chars.strip())
        if target_chars is not None and (
            isinstance(target_chars, bool) or not isinstance(target_chars, int) or target_chars < 1
        ):
            target_chars = None
            target_chars_warning = True
        if not isinstance(overwrite, bool):
            raise NovelWritingError("overwrite must be a boolean")

        calls: list[dict[str, Any]] = []
        with ProjectLock(
            self.project_root,
            timeout=self.lock_timeout_seconds,
            require_marker=False,
        ):
            existing = self._chapter_candidates(chapter)
            if existing and not overwrite:
                raise ChapterAlreadyExistsError(
                    f"chapter {chapter} already exists; explicit overwrite is required"
                )
            existing_snapshot = self._chapter_snapshot(existing)
            state_path = self.project_root / ".webnovel" / "state.json"
            state_snapshot = (
                state_path.read_bytes()
                if state_path.is_file() and not state_path.is_symlink()
                else None
            )
            context = self.context_builder.build(
                chapter, author_instruction=author_instruction
            )
            title = self._safe_title(context.title, chapter)
            chapter_path = self._chapter_path(chapter, title)
            draft = self._draft(context, target_chars=target_chars, calls=calls)
            final_body, editor_warning = self._edit(context, draft, calls=calls)
            current = self._chapter_candidates(chapter)
            current_snapshot = self._chapter_snapshot(current)
            if current_snapshot != existing_snapshot:
                if not overwrite and current:
                    raise ChapterAlreadyExistsError(
                        f"chapter {chapter} was created during generation; refusing overwrite"
                    )
                raise NovelWritingError(
                    f"chapter {chapter} changed during generation; rerun before overwrite"
                )
            if overwrite:
                self._backup_existing(existing)
            manuscript = f"# 第{chapter}章 {title}\n\n{final_body.strip()}\n"
            self._atomic_text(chapter_path, manuscript)
            cleanup_warning = False
            for old_path in existing:
                if old_path != chapter_path:
                    try:
                        old_path.unlink(missing_ok=True)
                    except OSError:
                        cleanup_warning = True
            if any(old_path != chapter_path for old_path in existing) and not cleanup_warning:
                self._fsync_directory(chapter_path.parent)

            warnings = list(context.warnings)
            if target_chars_warning:
                warnings.append("target_chars_ignored")
            if editor_warning:
                warnings.append(editor_warning)
            if cleanup_warning:
                warnings.append("legacy_cleanup_failed")
            extracted_memory: dict[str, Any] | None = None
            try:
                extracted_memory = self._memory_payload(
                    chapter=chapter,
                    title=title,
                    body=final_body,
                    calls=calls,
                )
                self._update_memory(chapter, extracted_memory)
                warnings = [
                    warning
                    for warning in warnings
                    if warning not in {"memory_missing", "memory_invalid"}
                ]
            except Exception:
                if calls and calls[-1].get("stage") == "memory":
                    calls[-1]["failed"] = True
                else:
                    calls.append({"stage": "memory", "attempt": 1, "failed": True})
                warnings.append("memory_failed")
            progress_warning = self._update_progress(
                chapter,
                expected_state=state_snapshot,
                title=title,
                body=final_body,
                memory=extracted_memory,
            )
            if progress_warning:
                warnings.append(progress_warning)

        warnings = list(dict.fromkeys(warnings))
        quality_lint = lint_prose(final_body, language=context.language)
        return {
            "status": "written_with_warnings" if warnings else "written",
            "chapter": chapter,
            "title": title,
            "chapter_file": str(chapter_path),
            "character_count": len(final_body),
            "language": context.language,
            "scene_beats": list(context.scene_beats),
            "active_entities": [item["name"] for item in context.active_entities],
            "foreshadowing_alerts": list(context.foreshadowing_alerts),
            "quality_lint": quality_lint,
            "warnings": warnings,
            "model_calls": calls,
            "next_command": f"/webnovel-write {chapter + 1}",
        }


__all__ = [
    "ChapterAlreadyExistsError",
    "NovelWritingError",
    "NovelWritingService",
    "UnsafeChapterPathError",
]

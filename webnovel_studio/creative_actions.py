"""Creative-first project initialization and volume planning."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .project_lock import ProjectLock


class CreativeActionError(RuntimeError):
    """A creative project action cannot safely complete."""


class CreativeOverwriteError(CreativeActionError):
    """A human-authored creative file is protected from implicit overwrite."""


_PENDING_INIT_SCHEMA = "novel-init-pending/v1"
_MAX_VOLUME_CHAPTERS = 500


_INIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "genre": {"type": "string"},
        "premise": {"type": "string"},
        "protagonist": {"type": "string"},
        "core_conflict": {"type": "string"},
        "world": {"type": "string"},
        "tone": {"type": "string"},
        "opening": {"type": "string"},
        "long_arc": {"type": "string"},
    },
    "required": [
        "title",
        "genre",
        "premise",
        "protagonist",
        "core_conflict",
        "world",
        "tone",
        "opening",
        "long_arc",
    ],
    "additionalProperties": True,
}

_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapter": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "event": {"type": "string"},
        "character_choice": {"type": "string"},
        "ending_drive": {"type": "string"},
    },
    "required": ["chapter", "title", "event", "character_choice", "ending_drive"],
    "additionalProperties": True,
}

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "volume_title": {"type": "string"},
        "volume_arc": {"type": "string"},
        "chapter_cards": {
            "type": "array",
            "maxItems": _MAX_VOLUME_CHAPTERS,
            "items": _CARD_SCHEMA,
        },
    },
    "required": ["volume_title", "volume_arc", "chapter_cards"],
    "additionalProperties": True,
}

_SUPPLEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chapter_cards": {
            "type": "array",
            "maxItems": _MAX_VOLUME_CHAPTERS,
            "items": _CARD_SCHEMA,
        },
    },
    "required": ["chapter_cards"],
    "additionalProperties": True,
}


class CreativeProjectService:
    """Turn ideas into human-readable project files and chapter cards."""

    def __init__(self, project_root: str | Path, *, router: Any) -> None:
        requested = Path(project_root).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        if any(component.is_symlink() for component in (requested, *requested.parents)):
            raise CreativeActionError("project path must not contain a symlink")
        self.project_root = requested.resolve(strict=False)
        self.router = router

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_text(cls, path: Path, text: str) -> None:
        cls._atomic_bytes(path, text.encode("utf-8"))

    @classmethod
    def _atomic_json(cls, path: Path, value: Mapping[str, Any]) -> None:
        cls._atomic_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _safe_directory(self, relative: str, *, create: bool = True) -> Path:
        path = self.project_root / relative
        if path.is_symlink():
            raise CreativeActionError(
                f"project directory contains an unsafe symlink: {relative}"
            )
        if path.exists() and not path.is_dir():
            raise CreativeActionError(f"project path is not a directory: {relative}")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        try:
            path.resolve(strict=create).relative_to(self.project_root)
        except (OSError, ValueError) as exc:
            raise CreativeActionError(
                f"project directory escapes project root: {relative}"
            ) from exc
        return path

    @staticmethod
    def _snapshot_file(path: Path) -> bytes | None:
        if path.is_symlink():
            raise CreativeActionError(f"project file must not be a symlink: {path.name}")
        if not path.exists():
            return None
        if not path.is_file():
            raise CreativeActionError(f"project path is not a file: {path.name}")
        return path.read_bytes()

    @classmethod
    def _transactional_text_writes(cls, writes: list[tuple[Path, str]]) -> None:
        snapshots = {path: cls._snapshot_file(path) for path, _ in writes}
        try:
            for path, text in writes:
                cls._atomic_text(path, text)
        except Exception:
            restore_errors: list[Exception] = []
            for path, original in reversed(list(snapshots.items())):
                try:
                    if original is None:
                        if path.is_symlink():
                            raise CreativeActionError(
                                f"cannot roll back unsafe project file: {path.name}"
                            )
                        path.unlink(missing_ok=True)
                    else:
                        cls._atomic_bytes(path, original)
                except Exception as exc:
                    restore_errors.append(exc)
            if restore_errors:
                raise CreativeActionError(
                    "creative file transaction rollback failed"
                ) from restore_errors[0]
            raise

    @classmethod
    def _read_state(cls, state_path: Path) -> dict[str, Any]:
        raw = cls._snapshot_file(state_path)
        if raw is None:
            raise CreativeActionError("project state is missing")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CreativeActionError("project state is invalid") from exc
        if not isinstance(value, dict):
            raise CreativeActionError("project state must be an object")
        return value

    @staticmethod
    def _is_pending_state(value: Mapping[str, Any]) -> bool:
        return value.get("schema_version") == _PENDING_INIT_SCHEMA and not (
            value.get("project_info") or value.get("project")
        )

    @staticmethod
    def _text(value: Any, fallback: str) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None:
            rendered = str(value).strip()
            if rendered:
                return rendered
        return fallback

    @classmethod
    def _single_line(cls, value: Any, fallback: str, *, limit: int) -> str:
        text = re.sub(r"\s+", " ", cls._text(value, fallback)).strip()
        text = re.sub(r"^#{1,6}\s*", "", text).strip()
        return text[:limit].rstrip()

    @staticmethod
    def _input_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, (list, tuple, set)):
            return "\n".join(str(item) for item in value)
        return str(value)

    def propose(self, idea: Any) -> dict[str, Any]:
        idea_text = self._input_text(idea)
        if not idea_text:
            idea_text = "请创造一部有鲜明人物、核心冲突和长期故事动力的中文长篇小说。"
        result = self.router.complete_structured(
            "architect",
            instructions=(
                "把作者灵感发展成一份完整但简洁的小说创作方案。重点是人物欲望、核心冲突、"
                "世界压力、叙事声音、开篇事件和能持续多卷的故事动力。不要列系统字段、"
                "验收规则、商业评分或工作流。"
            ),
            input=[{"type": "text", "text": idea_text}],
            json_schema=_INIT_SCHEMA,
            json_mode=True,
            schema_name="novel_proposal_v1",
            system_prompt=(
                "你是经验丰富的中文小说策划。创意必须具体、可写、有人物选择和长期冲突。"
            ),
            purpose="webnovel:creative:init",
        )
        candidate = getattr(result, "parsed", None)
        raw = candidate if isinstance(candidate, Mapping) else {}
        return {
            "title": self._text(raw.get("title"), "未命名小说"),
            "genre": self._text(raw.get("genre"), "未定题材"),
            "premise": self._text(raw.get("premise"), idea_text),
            "protagonist": self._text(raw.get("protagonist"), "由故事冲突塑造的主角"),
            "core_conflict": self._text(raw.get("core_conflict"), idea_text),
            "world": self._text(raw.get("world"), "围绕核心冲突展开的世界"),
            "tone": self._text(raw.get("tone"), "清晰、有张力、人物驱动"),
            "opening": self._text(raw.get("opening"), "主角被迫做出第一个不可回避的选择"),
            "long_arc": self._text(raw.get("long_arc"), "主角为核心目标持续行动并承担代价"),
        }

    def initialize(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {
            "title": self._text(proposal.get("title"), "未命名小说"),
            "genre": self._text(proposal.get("genre"), "未定题材"),
            "premise": self._text(proposal.get("premise"), "一个等待展开的故事"),
            "protagonist": self._text(proposal.get("protagonist"), "待故事塑造的主角"),
            "core_conflict": self._text(proposal.get("core_conflict"), "主角必须做出艰难选择"),
            "world": self._text(proposal.get("world"), "围绕核心冲突展开的世界"),
            "tone": self._text(proposal.get("tone"), "人物驱动"),
            "opening": self._text(proposal.get("opening"), "一个不可回避的事件发生"),
            "long_arc": self._text(proposal.get("long_arc"), "主角持续追寻目标并承担代价"),
        }
        existed_before = self.project_root.exists()
        if existed_before and not self.project_root.is_dir():
            raise CreativeActionError("project root is not a directory")
        if not existed_before:
            self.project_root.mkdir(parents=True, exist_ok=False)

        metadata = self.project_root / ".webnovel"
        if metadata.is_symlink():
            raise CreativeActionError("project directory contains an unsafe symlink: .webnovel")
        if metadata.exists() and not metadata.is_dir():
            raise CreativeActionError("project metadata path is not a directory")
        metadata.mkdir(parents=True, exist_ok=True)

        state_path = metadata / "state.json"

        def target_has_author_content() -> bool:
            for entry in self.project_root.iterdir():
                if entry.name == ".webnovel":
                    if any(
                        item.name not in {"state.json", "run.lock"}
                        for item in metadata.iterdir()
                    ):
                        return True
                    continue
                if entry.name in {"设定集", "大纲", "正文", "审查报告"}:
                    if entry.is_symlink() or (entry.is_dir() and any(entry.iterdir())):
                        return True
                    if not entry.is_dir():
                        return True
                    continue
                return True
            return False

        if existed_before and not state_path.exists() and target_has_author_content():
            raise CreativeOverwriteError("project target already contains author files")
        for directory in ("设定集", "大纲", "正文", "审查报告"):
            self._safe_directory(directory, create=False)

        with ProjectLock(
            self.project_root, timeout=10.0, require_marker=False
        ):
            if state_path.exists():
                try:
                    current = self._read_state(state_path)
                except CreativeActionError as exc:
                    raise CreativeOverwriteError(
                        "existing project state is not an init retry marker"
                    ) from exc
                if not self._is_pending_state(current):
                    raise CreativeOverwriteError("novel project changed during initialization")
                if target_has_author_content():
                    raise CreativeOverwriteError(
                        "pending initialization contains author files; refusing retry"
                    )
            else:
                if target_has_author_content():
                    raise CreativeOverwriteError("project target changed during initialization")
                self._atomic_json(
                    state_path, {"schema_version": _PENDING_INIT_SCHEMA}
                )
            current = self._read_state(state_path)
            if not self._is_pending_state(current):
                raise CreativeOverwriteError("novel project changed during initialization")
            for directory in ("设定集", "大纲", "正文", "审查报告"):
                self._safe_directory(directory)
            state = {
                "schema_version": "novel-project/v1",
                "project_info": {
                    "title": normalized["title"],
                    "genre": normalized["genre"],
                    "premise": normalized["premise"],
                },
                "progress": {
                    "current_chapter": 0,
                    "volumes_planned": [],
                },
            }
            settings = (
                f"# {normalized['title']}：核心设定\n\n"
                f"## 题材与叙事声音\n\n- 题材：{normalized['genre']}\n- 调性：{normalized['tone']}\n\n"
                f"## 主角\n\n{normalized['protagonist']}\n\n"
                f"## 世界\n\n{normalized['world']}\n\n"
                f"## 核心冲突\n\n{normalized['core_conflict']}\n"
            )
            outline = (
                f"# {normalized['title']}：总纲\n\n"
                f"## 一句话故事\n\n{normalized['premise']}\n\n"
                f"## 开篇事件\n\n{normalized['opening']}\n\n"
                f"## 长期故事动力\n\n{normalized['long_arc']}\n\n"
                f"## 核心人物与冲突\n\n{normalized['protagonist']}\n\n{normalized['core_conflict']}\n"
            )
            self._transactional_text_writes(
                [
                    (
                        self.project_root / ".webnovel" / "creative_proposal.json",
                        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)
                        + "\n",
                    ),
                    (self.project_root / "设定集" / "核心设定.md", settings),
                    (self.project_root / "大纲" / "总纲.md", outline),
                    (
                        state_path,
                        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )

        return {
            "status": "initialized",
            "project_root": str(self.project_root),
            "title": normalized["title"],
            "state_file": str(state_path),
            "settings_file": str(self.project_root / "设定集" / "核心设定.md"),
            "outline_file": str(self.project_root / "大纲" / "总纲.md"),
            "next_command": "/webnovel-plan 1",
        }

    def _project_sources(self) -> dict[str, str]:
        for directory in (".webnovel", "大纲", "设定集"):
            self._safe_directory(directory, create=False)
        sources: dict[str, str] = {}
        for relative in (
            Path(".webnovel/state.json"),
            Path(".webnovel/creative_proposal.json"),
            Path("大纲/总纲.md"),
            Path("设定集/核心设定.md"),
        ):
            path = self.project_root / relative
            if path.is_file() and not path.is_symlink():
                try:
                    sources[relative.as_posix()] = path.read_text(encoding="utf-8")[:12000]
                except (OSError, UnicodeError):
                    continue
        return sources

    @staticmethod
    def _cards(value: Any, *, start: int, end: int) -> dict[int, dict[str, Any]]:
        raw_cards = value.get("chapter_cards") if isinstance(value, Mapping) else None
        if not isinstance(raw_cards, list):
            return {}
        if len(raw_cards) > end - start + 1:
            raise CreativeActionError("planner returned more chapter cards than requested")
        cards: dict[int, dict[str, Any]] = {}
        for item in raw_cards:
            if not isinstance(item, Mapping):
                continue
            number = item.get("chapter")
            if isinstance(number, bool) or not isinstance(number, int) or not start <= number <= end:
                continue
            title = CreativeProjectService._single_line(
                item.get("title"), "", limit=120
            )
            event = CreativeProjectService._single_line(
                item.get("event"), "", limit=2000
            )
            character_choice = CreativeProjectService._single_line(
                item.get("character_choice"), "", limit=2000
            )
            ending_drive = CreativeProjectService._single_line(
                item.get("ending_drive"), "", limit=2000
            )
            if not all((title, event, character_choice, ending_drive)):
                continue
            cards[number] = {
                "chapter": number,
                "title": title,
                "event": event,
                "character_choice": character_choice,
                "ending_drive": ending_drive,
            }
        return cards

    def plan_volume(
        self,
        volume: int,
        *,
        start_chapter: int | None = None,
        end_chapter: int | None = None,
        requirements: Any = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 1:
            raise CreativeActionError("volume must be a positive integer")
        start = start_chapter or (volume - 1) * 30 + 1
        end = end_chapter or volume * 30
        if start < 1 or end < start:
            raise CreativeActionError("chapter range is invalid")
        if end - start + 1 > _MAX_VOLUME_CHAPTERS:
            raise CreativeActionError(
                f"one volume may plan at most {_MAX_VOLUME_CHAPTERS} chapters"
            )
        state_path = self.project_root / ".webnovel" / "state.json"
        if not state_path.is_file() or state_path.is_symlink():
            raise CreativeActionError("project is not initialized")
        self._read_state(state_path)
        outline_directory = self._safe_directory("大纲", create=False)
        if not outline_directory.is_dir():
            raise CreativeActionError("project outline directory is missing")
        outline_path = outline_directory / f"第{volume}卷-详细大纲.md"
        beat_path = outline_directory / f"第{volume}卷-节拍表.md"
        target_snapshots = {
            outline_path: self._snapshot_file(outline_path),
            beat_path: self._snapshot_file(beat_path),
        }
        if (outline_path.exists() or beat_path.exists()) and not overwrite:
            raise CreativeOverwriteError("volume plan already exists; explicit overwrite is required")

        source_paths = (
            state_path,
            self.project_root / ".webnovel" / "creative_proposal.json",
            self.project_root / "大纲" / "总纲.md",
            self.project_root / "设定集" / "核心设定.md",
        )
        source_snapshots = {path: self._snapshot_file(path) for path in source_paths}

        request = {
            "volume": volume,
            "start_chapter": start,
            "end_chapter": end,
            "requirements": self._input_text(requirements),
            "project_sources": self._project_sources(),
        }
        result = self.router.complete_structured(
            "architect",
            instructions=(
                "规划一卷真正可写的小说。每章只给章名、本章实际事件、人物必须做出的选择、"
                "以及由本章事件自然产生的结尾推动。关注剧情因果、人物关系、节奏和情绪递进。"
                "不要生成时间字段清单、节点编码、逐字锚点、审查规则或合同。"
            ),
            input=[
                {
                    "type": "text",
                    "text": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            json_schema=_PLAN_SCHEMA,
            json_mode=True,
            schema_name="novel_volume_plan_v1",
            system_prompt="你是长篇小说卷纲策划，所有章节必须能直接拿来写场景。",
            purpose="webnovel:creative:plan",
        )
        candidate = getattr(result, "parsed", None)
        raw_plan = candidate if isinstance(candidate, Mapping) else {}
        cards = self._cards(raw_plan, start=start, end=end)
        for _ in range(2):
            missing = [number for number in range(start, end + 1) if number not in cards]
            if not missing:
                break
            supplement = self.router.complete_structured(
                "architect",
                instructions=(
                    "只补齐指定缺失章节，不重写或复述已经存在的章节。每章给章名、实际事件、"
                    "人物选择和结尾推动。"
                ),
                input=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "missing_chapters": missing,
                                "existing_cards": list(cards.values()),
                                "volume_arc": raw_plan.get("volume_arc", ""),
                                "requirements": self._input_text(requirements),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                ],
                json_schema=_SUPPLEMENT_SCHEMA,
                json_mode=True,
                schema_name="novel_volume_plan_supplement_v1",
                system_prompt="你是长篇小说卷纲策划，只补缺失章节。",
                purpose="webnovel:creative:plan:supplement",
            )
            supplemental_cards = self._cards(
                getattr(supplement, "parsed", None), start=start, end=end
            )
            for number in missing:
                if number in supplemental_cards:
                    cards[number] = supplemental_cards[number]

        missing = [number for number in range(start, end + 1) if number not in cards]
        if missing:
            raise CreativeActionError(
                f"planner did not return usable chapter cards: {missing}"
            )

        volume_title = self._single_line(
            raw_plan.get("volume_title"), f"第{volume}卷", limit=120
        )
        volume_arc = self._single_line(
            raw_plan.get("volume_arc"),
            "人物在连续选择与后果中推动本卷核心冲突。",
            limit=4000,
        )
        lines = [
            f"# 第{volume}卷 {volume_title}",
            "",
            "## 本卷人物与剧情变化",
            "",
            volume_arc,
            "",
        ]
        for number in range(start, end + 1):
            card = cards[number]
            lines.extend(
                [
                    f"### 第{number}章 {card['title']}",
                    "",
                    f"- 本章事件：{card['event']}",
                    f"- 人物选择：{card['character_choice']}",
                    f"- 结尾推动：{card['ending_drive']}",
                    "",
                ]
            )
        outline_text = "\n".join(lines).rstrip() + "\n"
        beat_text = (
            f"# 第{volume}卷节拍表：{volume_title}\n\n"
            f"## 本卷人物与剧情变化\n\n{volume_arc}\n\n"
            f"## 章节范围\n\n第{start}章至第{end}章。\n"
        )

        with ProjectLock(self.project_root, timeout=10.0):
            self._safe_directory("大纲", create=False)
            for path, original in target_snapshots.items():
                if self._snapshot_file(path) != original:
                    raise CreativeOverwriteError(
                        "volume plan changed during generation; rerun without overwriting"
                    )
            for path, original in source_snapshots.items():
                if self._snapshot_file(path) != original:
                    raise CreativeOverwriteError(
                        "project sources changed during generation; rerun planning"
                    )
            state = self._read_state(state_path)
            progress = state.get("progress")
            if not isinstance(progress, dict):
                progress = {}
            planned = progress.get("volumes_planned")
            if not isinstance(planned, list):
                planned = []
            progress["volumes_planned"] = sorted(
                {int(item) for item in planned if isinstance(item, int) and item > 0} | {volume}
            )
            state["progress"] = progress
            self._transactional_text_writes(
                [
                    (outline_path, outline_text),
                    (beat_path, beat_text),
                    (
                        state_path,
                        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    ),
                ]
            )

        return {
            "status": "planned",
            "volume": volume,
            "start_chapter": start,
            "end_chapter": end,
            "outline_file": str(outline_path),
            "beat_file": str(beat_path),
            "chapter_count": end - start + 1,
            "next_command": f"/webnovel-write {start}",
        }


__all__ = [
    "CreativeActionError",
    "CreativeOverwriteError",
    "CreativeProjectService",
]

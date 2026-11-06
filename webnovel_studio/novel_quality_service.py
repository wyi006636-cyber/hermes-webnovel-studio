"""Optional, non-blocking reviews of novel craft."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .project_lock import ProjectLock, ProjectLockError


class NovelQualityError(RuntimeError):
    """The optional quality review request is invalid."""


class NovelQualityService:
    """Review prose as fiction without deciding whether it may exist."""

    QUALITY_GUIDE = (
        "只评论小说本身。依次关注：剧情是否由人物行动真正推进，因果是否成立；人物动机与"
        "关系变化是否可信；对白是否符合身份、情绪并带有潜台词；场景张力和节奏是否有松紧；"
        "情绪是否通过动作、感官与细节逐层积累；叙述视角、人物声音、句式、意象和文学表达"
        "是否服务故事；章末是否自然地产生下一章阅读欲望。"
    )
    MAX_REVIEW_CHAPTERS = 200

    def __init__(self, project_root: str | Path, *, router: Any) -> None:
        requested = Path(project_root).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        if any(component.is_symlink() for component in (requested, *requested.parents)):
            raise NovelQualityError("novel project path must not contain a symlink")
        self.project_root = requested.resolve(strict=True)
        if not self.project_root.is_dir() or self.project_root.is_symlink():
            raise NovelQualityError("novel project root is unsafe")
        self.router = router
        self._safe_directory(Path(".webnovel"), label="project metadata")
        self.report_directory = self._safe_directory(
            Path("审查报告"), label="review report"
        )

    def _safe_directory(self, relative: Path, *, label: str) -> Path:
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise NovelQualityError(f"{label} directory must not be a symlink")
            if current.exists() and not current.is_dir():
                raise NovelQualityError(f"{label} path must be a directory")
            current.mkdir(exist_ok=True)
        try:
            current.resolve(strict=True).relative_to(self.project_root)
        except (OSError, ValueError) as exc:
            raise NovelQualityError(
                f"{label} directory escapes the novel project"
            ) from exc
        return current

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
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _chapter_file(self, chapter: int) -> Path | None:
        body_dir = self.project_root / "正文"
        if body_dir.is_symlink() or not body_dir.is_dir():
            return None
        candidates = sorted(body_dir.glob(f"第{chapter:04d}章-*.md"))
        if not candidates:
            candidates = sorted(body_dir.glob(f"第{chapter}章*.md"))
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                try:
                    candidate.resolve(strict=True).relative_to(self.project_root)
                except ValueError:
                    continue
                return candidate
        return None

    @staticmethod
    def _clean_report(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise NovelQualityError("reviewer returned no useful report")
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        if not text:
            raise NovelQualityError("reviewer returned no useful report")
        return text

    def review_range(self, start_chapter: int, end_chapter: int) -> dict[str, Any]:
        for name, value in (("start_chapter", start_chapter), ("end_chapter", end_chapter)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise NovelQualityError(f"{name} must be a positive integer")
        if end_chapter < start_chapter:
            raise NovelQualityError("end_chapter must not precede start_chapter")
        if end_chapter - start_chapter + 1 > self.MAX_REVIEW_CHAPTERS:
            raise NovelQualityError(
                f"one review may include at most {self.MAX_REVIEW_CHAPTERS} chapters"
            )

        reports: list[str] = []
        warnings: list[str] = []
        for chapter in range(start_chapter, end_chapter + 1):
            chapter_path = self._chapter_file(chapter)
            if chapter_path is None:
                warnings.append(f"chapter_{chapter}_missing")
                continue
            try:
                manuscript_bytes = chapter_path.read_bytes()
                manuscript = manuscript_bytes.decode("utf-8")
                result = self.router.complete(
                    "reader_editor",
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是严肃、具体、尊重作者意图的中文小说编辑。"
                                + self.QUALITY_GUIDE
                                + "不判定通过或失败，不要求重写全文。先指出真正有效的部分，"
                                "再给少量能直接落到场景、对白或叙述上的修改建议。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"请审读第{chapter}章。报告使用 Markdown，包含“有效之处”、"
                                f"“可以提升”和“可直接尝试的修改”三部分。\n\n{manuscript}"
                            ),
                        },
                    ],
                    purpose="webnovel:novel:quality-review",
                )
                report = self._clean_report(getattr(result, "text", None))
                report_path = self.report_directory / f"第{chapter:04d}章-小说质量报告.md"
                report_text = f"# 第{chapter}章小说质量报告\n\n{report}\n"
                with ProjectLock(
                    self.project_root, timeout=10.0, require_marker=False
                ):
                    self._safe_directory(Path("审查报告"), label="review report")
                    if (
                        chapter_path.is_symlink()
                        or not chapter_path.is_file()
                        or chapter_path.read_bytes() != manuscript_bytes
                    ):
                        raise NovelQualityError(
                            f"chapter {chapter} changed during review; report discarded"
                        )
                    self._atomic_text(report_path, report_text)
                reports.append(str(report_path))
            except ProjectLockError:
                raise
            except Exception:
                warnings.append(f"chapter_{chapter}_review_failed")

        return {
            "status": "reviewed_with_warnings" if warnings else "reviewed",
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
            "reports": reports,
            "warnings": warnings,
            "manuscripts_modified": False,
        }


__all__ = ["NovelQualityError", "NovelQualityService"]

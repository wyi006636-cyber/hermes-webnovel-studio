from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from webnovel_studio.novel_writing_service import (
    ChapterAlreadyExistsError,
    NovelWritingError,
    NovelWritingService,
    UnsafeChapterPathError,
)


class FakeRouter:
    def __init__(
        self,
        *,
        draft: str = "林雾推开舱门。",
        edited: str | Exception = "舱门缓慢开启，林雾听见风暴深处有人敲击船壳。",
        memory: dict[str, Any] | Exception | None = None,
    ) -> None:
        self.draft = draft
        self.edited = edited
        self.memory = memory or {
            "chapter": 1,
            "title": "失火的信标",
            "summary": "林雾进入信标站并听见异常敲击。",
            "character_changes": ["林雾决定继续深入"],
            "open_threads": ["敲击者是谁"],
            "ending": "敲击声再次响起。",
        }
        self.calls: list[tuple[str, str]] = []

    def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
        self.calls.append((role, purpose or ""))
        if purpose == "webnovel:novel:draft":
            return SimpleNamespace(
                text=self.draft,
                provider="test-provider",
                model="test-model",
                usage={},
                audit={},
            )
        if purpose == "webnovel:novel:edit":
            if isinstance(self.edited, Exception):
                raise self.edited
            return SimpleNamespace(
                text=self.edited,
                provider="test-provider",
                model="test-model",
                usage={},
                audit={},
            )
        raise AssertionError(purpose)

    def complete_structured(self, role: str, **kwargs: Any) -> Any:
        purpose = str(kwargs.get("purpose") or "")
        self.calls.append((role, purpose))
        if isinstance(self.memory, Exception):
            raise self.memory
        return SimpleNamespace(parsed=self.memory)


def make_project(root: Path) -> Path:
    (root / ".webnovel").mkdir(parents=True)
    (root / "大纲").mkdir()
    (root / "设定集").mkdir()
    (root / "正文").mkdir()
    (root / ".webnovel" / "state.json").write_text(
        json.dumps({"project_info": {"title": "潮汐尽头", "genre": "科幻"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "大纲" / "第1卷-详细大纲.md").write_text(
        "### 第1章 失火的信标\n\n林雾进入废弃信标站。\n",
        encoding="utf-8",
    )
    (root / "设定集" / "人物.md").write_text("林雾：克制、敏锐。\n", encoding="utf-8")
    return root


def test_short_chapter_is_saved_without_word_count_gate(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeRouter(draft="她醒了。", edited="她在警报声中睁开眼。")

    result = NovelWritingService(root, router=router).write_chapter(1, target_chars=3000)

    chapter = Path(result["chapter_file"])
    assert result["status"] == "written"
    assert chapter.is_file()
    assert chapter.read_text(encoding="utf-8").strip().endswith("她在警报声中睁开眼。")
    assert result["character_count"] < 3000
    assert "word_count" not in " ".join(result["warnings"])
    assert len(router.calls) == 3
    state = json.loads((root / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    assert state["progress"]["current_chapter"] == 1


def test_editor_failure_keeps_draft_and_memory_failure_never_blocks_body(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeRouter(
        draft="林雾推开舱门。",
        edited=TimeoutError("editor unavailable"),
        memory=TimeoutError("memory unavailable"),
    )

    result = NovelWritingService(root, router=router).write_chapter(1)

    chapter = Path(result["chapter_file"])
    assert result["status"] == "written_with_warnings"
    assert chapter.read_text(encoding="utf-8").strip().endswith("林雾推开舱门。")
    assert "editor_failed" in result["warnings"]
    assert "memory_failed" in result["warnings"]
    assert len(router.calls) == 3
    assert [call["stage"] for call in result["model_calls"]] == [
        "draft",
        "edit",
        "memory",
    ]
    assert result["model_calls"][1]["failed"] is True
    assert result["model_calls"][2]["failed"] is True


def test_invalid_draft_gets_exactly_one_technical_retry(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")

    class RetryRouter(FakeRouter):
        def __init__(self) -> None:
            super().__init__(edited="编辑后的有效正文。")
            self.drafts = ["{}", "第二次返回有效小说正文。"]

        def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
            if purpose == "webnovel:novel:draft":
                self.calls.append((role, purpose))
                return SimpleNamespace(text=self.drafts.pop(0))
            return super().complete(role, messages, purpose=purpose)

    router = RetryRouter()
    result = NovelWritingService(root, router=router).write_chapter(1)

    assert Path(result["chapter_file"]).is_file()
    assert router.calls.count(("writer", "webnovel:novel:draft")) == 2
    assert [
        (call["stage"], call["attempt"], call.get("failed", False))
        for call in result["model_calls"]
    ] == [
        ("draft", 1, True),
        ("draft", 2, False),
        ("edit", 1, False),
        ("memory", 1, False),
    ]


def test_two_invalid_drafts_stop_without_publishing_or_third_attempt(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")

    class ExhaustedRouter(FakeRouter):
        def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
            del messages
            self.calls.append((role, purpose or ""))
            return SimpleNamespace(text="{}")

    router = ExhaustedRouter()
    with pytest.raises(NovelWritingError, match="failed to produce usable"):
        NovelWritingService(root, router=router).write_chapter(1)

    assert router.calls == [
        ("writer", "webnovel:novel:draft"),
        ("writer", "webnovel:novel:draft"),
    ]
    assert list((root / "正文").glob("*.md")) == []


def test_existing_chapter_requires_explicit_overwrite(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    existing = root / "正文" / "第0001章-旧标题.md"
    existing.write_text("作者手改正文", encoding="utf-8")
    router = FakeRouter()

    with pytest.raises(ChapterAlreadyExistsError):
        NovelWritingService(root, router=router).write_chapter(1)

    assert existing.read_text(encoding="utf-8") == "作者手改正文"
    assert router.calls == []


def test_legacy_unpadded_chapter_requires_confirmation_then_is_backed_up(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    existing = root / "正文" / "第1章 旧稿.md"
    original = "作者手改的旧格式正文"
    existing.write_text(original, encoding="utf-8")

    refused_router = FakeRouter()
    with pytest.raises(ChapterAlreadyExistsError):
        NovelWritingService(root, router=refused_router).write_chapter(1)
    assert existing.read_text(encoding="utf-8") == original
    assert refused_router.calls == []

    accepted_router = FakeRouter(draft="新正文。", edited="编辑后的新正文。")
    result = NovelWritingService(root, router=accepted_router).write_chapter(1, overwrite=True)

    current = list((root / "正文").glob("第*章*.md"))
    backups = list((root / ".webnovel" / "backups" / "chapters").glob("*-第1章 旧稿.md"))
    assert len(current) == 1
    assert Path(result["chapter_file"]) == current[0]
    assert "编辑后的新正文" in current[0].read_text(encoding="utf-8")
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_backup_destination_symlink_is_rejected_without_copying_or_overwrite(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    existing = root / "正文" / "第1章 作者原稿.md"
    original = "作者不能被覆盖的正文"
    existing.write_text(original, encoding="utf-8")
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    try:
        (root / ".webnovel" / "backups").symlink_to(
            outside, target_is_directory=True
        )
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(UnsafeChapterPathError, match="backup.*symlink"):
        NovelWritingService(root, router=FakeRouter()).write_chapter(
            1, overwrite=True
        )

    assert existing.read_text(encoding="utf-8") == original
    assert list(outside.iterdir()) == []


def test_repeated_overwrite_keeps_each_prior_author_version_in_distinct_backup(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    legacy = root / "正文" / "第1章 旧稿.md"
    legacy.write_text("最初作者版本", encoding="utf-8")

    first = NovelWritingService(
        root, router=FakeRouter(draft="第一轮。", edited="第一轮编辑正文。")
    ).write_chapter(1, overwrite=True)
    first_bytes = Path(first["chapter_file"]).read_bytes()
    NovelWritingService(
        root, router=FakeRouter(draft="第二轮。", edited="第二轮编辑正文。")
    ).write_chapter(1, overwrite=True)

    backups = sorted((root / ".webnovel" / "backups" / "chapters").glob("*.md"))
    assert len(backups) == 2
    backup_contents = {path.read_bytes() for path in backups}
    assert "最初作者版本".encode() in backup_contents
    assert first_bytes in backup_contents


def test_legacy_cleanup_failure_keeps_successful_new_prose_and_returns_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path / "book")
    legacy = root / "正文" / "第1章 旧稿.md"
    legacy.write_text("不能丢的旧稿", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_legacy_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == legacy:
            raise OSError("injected cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_legacy_unlink)
    result = NovelWritingService(
        root, router=FakeRouter(draft="新稿。", edited="清理失败后的新稿。")
    ).write_chapter(1, overwrite=True)

    assert result["status"] == "written_with_warnings"
    assert "legacy_cleanup_failed" in result["warnings"]
    assert legacy.read_text(encoding="utf-8") == "不能丢的旧稿"
    assert "清理失败后的新稿" in Path(result["chapter_file"]).read_text(encoding="utf-8")
    backups = list((root / ".webnovel" / "backups" / "chapters").glob("*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "不能丢的旧稿"


def test_invalid_target_chars_becomes_warning_without_blocking_prose(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeRouter(draft="正文继续发生。", edited="正文继续发生。")

    result = NovelWritingService(root, router=router).write_chapter(
        1, target_chars="不是数字"  # type: ignore[arg-type]
    )

    assert Path(result["chapter_file"]).is_file()
    assert "target_chars_ignored" in result["warnings"]
    assert result["status"] == "written_with_warnings"


def test_human_story_files_can_produce_prose_without_state_json(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    (root / ".webnovel" / "state.json").unlink()
    router = FakeRouter(draft="她推开没有编号的门。", edited="她推开没有编号的门。")

    result = NovelWritingService(root, router=router).write_chapter(1)

    assert Path(result["chapter_file"]).is_file()
    assert "state_missing" in result["warnings"]


def test_author_edit_during_generation_is_never_overwritten(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    existing = root / "正文" / "第1章-作者实时修改.md"
    existing.write_text("# 第1章\n\n作者刚刚写下的新段落。\n", encoding="utf-8")

    class EditingRouter(FakeRouter):
        def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
            if purpose == "webnovel:novel:draft":
                existing.write_text(
                    "# 第1章\n\n作者在模型生成期间继续修改。\n", encoding="utf-8"
                )
            return super().complete(role, messages, purpose=purpose)

    with pytest.raises(NovelWritingError, match="changed during generation"):
        NovelWritingService(root, router=EditingRouter()).write_chapter(
            1, overwrite=True
        )

    assert "作者在模型生成期间继续修改" in existing.read_text(encoding="utf-8")
    assert not list((root / ".webnovel" / "backups").glob("**/*.md"))


def test_author_created_chapter_during_generation_is_protected_without_overwrite(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    author_file = root / "正文" / "第0001章-失火的信标.md"

    class CreatingRouter(FakeRouter):
        def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
            if purpose == "webnovel:novel:draft":
                author_file.write_text(
                    "# 第1章 失火的信标\n\n作者在生成期间新写的正文。\n",
                    encoding="utf-8",
                )
            return super().complete(role, messages, purpose=purpose)

    with pytest.raises(ChapterAlreadyExistsError, match="created during generation"):
        NovelWritingService(root, router=CreatingRouter()).write_chapter(1)

    assert "作者在生成期间新写的正文" in author_file.read_text(encoding="utf-8")


def test_author_created_chapter_during_generation_is_protected_with_overwrite(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    author_file = root / "正文" / "第0001章-失火的信标.md"

    class CreatingRouter(FakeRouter):
        def complete(self, role: str, messages: Any, *, purpose: str | None = None) -> Any:
            if purpose == "webnovel:novel:draft":
                author_file.write_text(
                    "# 第1章 失火的信标\n\n作者并发创建且允许覆盖参数不能吞掉它。\n",
                    encoding="utf-8",
                )
            return super().complete(role, messages, purpose=purpose)

    with pytest.raises(NovelWritingError, match="changed during generation"):
        NovelWritingService(root, router=CreatingRouter()).write_chapter(
            1, overwrite=True
        )

    assert "允许覆盖参数不能吞掉它" in author_file.read_text(encoding="utf-8")
    assert not list((root / ".webnovel" / "backups").glob("**/*.md"))


def test_quality_prompt_focuses_on_novel_not_engineering_compliance(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeRouter()

    NovelWritingService(root, router=router).write_chapter(1)

    # Production service exposes the editor rubric for audit/testing without
    # turning it into a blocking scorecard.
    rubric = NovelWritingService.QUALITY_RUBRIC
    for dimension in ("剧情", "对白", "节奏", "情绪", "人物", "文学表达", "章末"):
        assert dimension in rubric
    for forbidden in (
        "字数审查",
        "source_id",
        "schema",
        "SHA",
        "CBN",
        "CPN",
        "CEN",
        "向量",
        "投影",
    ):
        assert forbidden not in rubric

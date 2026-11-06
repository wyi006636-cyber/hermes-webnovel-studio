from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from webnovel_studio.novel_quality_service import NovelQualityError, NovelQualityService
from webnovel_studio.project_lock import ProjectLockError


class FakeReviewRouter:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[dict[str, str]], str]] = []

    def complete(
        self,
        role: str,
        messages: list[dict[str, str]],
        *,
        purpose: str,
    ) -> Any:
        self.calls.append((role, messages, purpose))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(text=value)


def make_project(root: Path) -> Path:
    (root / ".webnovel").mkdir(parents=True)
    (root / "正文").mkdir()
    (root / ".webnovel" / "state.json").write_text(
        json.dumps({"project_info": {"title": "潮汐尽头"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "正文" / "第0001章-失火的信标.md").write_text(
        "# 第1章 失火的信标\n\n林雾推开舱门，周野没有跟上。\n",
        encoding="utf-8",
    )
    return root


def test_quality_review_only_reports_novel_craft_and_never_changes_manuscript(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path / "book")
    chapter = root / "正文" / "第0001章-失火的信标.md"
    before = chapter.read_bytes()
    router = FakeReviewRouter(
        [
            "## 有效之处\n对白克制。\n\n## 可以提升\n让周野的沉默产生更具体的后果。"
        ]
    )

    result = NovelQualityService(root, router=router).review_range(1, 1)

    assert result["status"] == "reviewed"
    assert chapter.read_bytes() == before
    report = Path(result["reports"][0]).read_text(encoding="utf-8")
    assert "对白克制" in report
    prompt = router.calls[0][1][0]["content"] + router.calls[0][1][1]["content"]
    for dimension in ("剧情", "人物动机", "对白", "节奏", "情绪", "文学表达", "章末"):
        assert dimension in prompt
    for irrelevant in ("字数达标", "SHA", "source_id", "CBN", "schema 合规", "投影"):
        assert irrelevant not in prompt


def test_review_range_is_bounded_before_any_model_call(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeReviewRouter(["不应调用"])
    service = NovelQualityService(root, router=router)

    with pytest.raises(NovelQualityError, match="at most 200"):
        service.review_range(1, 201)

    assert router.calls == []


def test_review_failure_is_warning_not_a_blocking_verdict(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    router = FakeReviewRouter([TimeoutError("review unavailable")])

    result = NovelQualityService(root, router=router).review_range(1, 1)

    assert result["status"] == "reviewed_with_warnings"
    assert result["warnings"] == ["chapter_1_review_failed"]
    assert result["reports"] == []


def test_symlinked_report_directory_is_rejected_without_external_write(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    try:
        (root / "审查报告").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(NovelQualityError, match="review report.*symlink"):
        NovelQualityService(root, router=FakeReviewRouter(["不应调用"]))

    assert list(outside.iterdir()) == []


def test_review_discards_stale_report_when_author_changes_manuscript(tmp_path: Path) -> None:
    root = make_project(tmp_path / "book")
    chapter = root / "正文" / "第0001章-失火的信标.md"

    class EditingRouter(FakeReviewRouter):
        def complete(
            self,
            role: str,
            messages: list[dict[str, str]],
            *,
            purpose: str,
        ) -> Any:
            chapter.write_text(
                "# 第1章 失火的信标\n\n作者审读期间写入的新版本。\n",
                encoding="utf-8",
            )
            return super().complete(role, messages, purpose=purpose)

    result = NovelQualityService(
        root, router=EditingRouter(["## 有效之处\n旧版本评价。"])
    ).review_range(1, 1)

    assert result["status"] == "reviewed_with_warnings"
    assert result["warnings"] == ["chapter_1_review_failed"]
    assert result["reports"] == []
    assert "作者审读期间写入的新版本" in chapter.read_text(encoding="utf-8")
    assert not (root / "审查报告" / "第0001章-小说质量报告.md").exists()


def test_review_does_not_hide_project_lock_failure_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import novel_quality_service

    root = make_project(tmp_path / "book")

    class BusyLock:
        def __init__(
            self, project_root: Path, *, timeout: float, require_marker: bool
        ) -> None:
            del project_root, timeout, require_marker

        def __enter__(self) -> None:
            raise ProjectLockError("busy")

        def __exit__(self, *args: Any) -> None:
            del args

    monkeypatch.setattr(novel_quality_service, "ProjectLock", BusyLock)
    router = FakeReviewRouter(["## 有效之处\n正文有效。"])

    with pytest.raises(ProjectLockError, match="busy"):
        NovelQualityService(root, router=router).review_range(1, 1)

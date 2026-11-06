from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from webnovel_studio.creative_actions import (
    CreativeActionError,
    CreativeOverwriteError,
    CreativeProjectService,
)
from webnovel_studio.novel_context import NovelContextBuilder


class FakeCreativeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.plan_calls = 0

    def complete_structured(self, role: str, **kwargs: Any) -> Any:
        purpose = str(kwargs.get("purpose") or "")
        self.calls.append((role, purpose, kwargs.get("input")))
        if purpose == "webnovel:creative:init":
            return SimpleNamespace(
                parsed={
                    "title": "潮汐尽头",
                    "genre": "星际悬疑",
                    "premise": "失忆领航员寻找一支被所有数据库抹去的舰队。",
                    "protagonist": "林雾，克制敏锐，但害怕再次相信同伴。",
                    "core_conflict": "林雾必须依赖可能背叛过她的人找回舰队。",
                    "world": "潮汐风暴切断星际航道，旧信标维持文明联系。",
                    "tone": "克制、紧张、有人情温度",
                    "opening": "废弃信标在停机十年后呼叫林雾的名字。",
                    "long_arc": "林雾追踪信标、重组船员并发现舰队主动消失的原因。",
                }
            )
        if purpose == "webnovel:creative:plan":
            self.plan_calls += 1
            return SimpleNamespace(
                parsed={
                    "volume_title": "失火的信标",
                    "volume_arc": "林雾从拒绝同行到重新建立最低限度的信任。",
                    "chapter_cards": [
                        {
                            "chapter": 1,
                            "title": "十年后的呼叫",
                            "event": "林雾收到废弃信标的点名呼叫并决定登站。",
                            "character_choice": "她隐瞒呼叫内容，独自行动。",
                            "ending_drive": "舱门内侧留着她失踪妹妹的新鲜血迹。",
                        },
                        {
                            "chapter": 3,
                            "title": "被删去的人",
                            "event": "林雾发现周野的身份也被数据库抹除。",
                            "character_choice": "她暂时不拆穿周野。",
                            "ending_drive": "周野主动说出妹妹的旧代号。",
                        },
                    ],
                }
            )
        if purpose == "webnovel:creative:plan:supplement":
            return SimpleNamespace(
                parsed={
                    "chapter_cards": [
                        {
                            "chapter": 2,
                            "title": "沉默航道",
                            "event": "林雾与周野穿过失效航道，发现坐标被人为篡改。",
                            "character_choice": "林雾允许周野同行但不交出控制权。",
                            "ending_drive": "导航图上出现一条只有妹妹能生成的航线。",
                        }
                    ]
                }
            )
        raise AssertionError(purpose)


def test_init_proposal_writes_nothing_before_one_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "new-book"
    router = FakeCreativeRouter()
    service = CreativeProjectService(root, router=router)

    proposal = service.propose("一个失忆领航员寻找消失舰队的故事")

    assert proposal["title"] == "潮汐尽头"
    assert not root.exists()
    assert [call[1] for call in router.calls] == ["webnovel:creative:init"]


def test_confirmed_init_writes_only_human_story_files_and_minimal_state(tmp_path: Path) -> None:
    root = tmp_path / "new-book"
    router = FakeCreativeRouter()
    service = CreativeProjectService(root, router=router)
    proposal = service.propose("一个失忆领航员寻找消失舰队的故事")

    result = service.initialize(proposal)

    assert result["status"] == "initialized"
    assert (root / ".webnovel" / "state.json").is_file()
    assert (root / "设定集" / "核心设定.md").is_file()
    assert (root / "大纲" / "总纲.md").is_file()
    assert (root / "正文").is_dir()
    assert not (root / ".story-system").exists()
    state = json.loads((root / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    assert state["project_info"]["title"] == "潮汐尽头"


def test_plan_accepts_text_requirements_and_supplements_only_missing_chapters(tmp_path: Path) -> None:
    root = tmp_path / "book"
    router = FakeCreativeRouter()
    service = CreativeProjectService(root, router=router)
    proposal = service.propose("一个失忆领航员寻找消失舰队的故事")
    service.initialize(proposal)

    result = service.plan_volume(
        1,
        start_chapter=1,
        end_chapter=3,
        requirements="对白克制，情绪靠行动推进",
    )

    assert result["status"] == "planned"
    outline = Path(result["outline_file"]).read_text(encoding="utf-8")
    assert outline.count("### 第") == 3
    assert "### 第1章 十年后的呼叫" in outline
    assert "### 第2章 沉默航道" in outline
    assert "### 第3章 被删去的人" in outline
    assert router.plan_calls == 1
    purposes = [call[1] for call in router.calls]
    assert purposes.count("webnovel:creative:plan:supplement") == 1
    assert not (root / ".story-system").exists()


def test_init_refuses_legacy_or_unknown_existing_state_without_touching_author_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-book"
    (root / ".webnovel").mkdir(parents=True)
    (root / "设定集").mkdir()
    (root / "大纲").mkdir()
    state = root / ".webnovel" / "state.json"
    settings = root / "设定集" / "核心设定.md"
    outline = root / "大纲" / "总纲.md"
    state.write_text('{"project":{"title":"旧书"}}', encoding="utf-8")
    settings.write_text("作者旧设定", encoding="utf-8")
    outline.write_text("作者旧总纲", encoding="utf-8")

    with pytest.raises(CreativeOverwriteError):
        CreativeProjectService(root, router=FakeCreativeRouter()).initialize(
            {"title": "新书"}
        )

    assert settings.read_text(encoding="utf-8") == "作者旧设定"
    assert outline.read_text(encoding="utf-8") == "作者旧总纲"
    assert json.loads(state.read_text(encoding="utf-8"))["project"]["title"] == "旧书"


def test_plan_rejects_symlinked_outline_directory(tmp_path: Path) -> None:
    root = tmp_path / "book"
    service = CreativeProjectService(root, router=FakeCreativeRouter())
    service.initialize(service.propose("星际悬疑"))
    (root / "大纲" / "总纲.md").unlink()
    (root / "大纲").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "大纲").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CreativeActionError, match="symlink"):
        service.plan_volume(1, start_chapter=1, end_chapter=3)

    assert list(outside.iterdir()) == []


def test_plan_supplement_cannot_replace_existing_cards(tmp_path: Path) -> None:
    class ReplacementRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            if kwargs.get("purpose") == "webnovel:creative:plan:supplement":
                self.calls.append((role, str(kwargs["purpose"]), kwargs.get("input")))
                return SimpleNamespace(
                    parsed={
                        "chapter_cards": [
                            {
                                "chapter": 1,
                                "title": "恶意替换",
                                "event": "覆盖第一章",
                                "character_choice": "覆盖",
                                "ending_drive": "覆盖",
                            },
                            {
                                "chapter": 2,
                                "title": "沉默航道",
                                "event": "林雾发现坐标被人为篡改。",
                                "character_choice": "她允许周野同行但不交出控制权。",
                                "ending_drive": "妹妹的航线在导航图上出现。",
                            },
                            {
                                "chapter": 3,
                                "title": "再次恶意替换",
                                "event": "覆盖第三章",
                                "character_choice": "覆盖",
                                "ending_drive": "覆盖",
                            },
                        ]
                    }
                )
            return super().complete_structured(role, **kwargs)

    root = tmp_path / "book"
    router = ReplacementRouter()
    service = CreativeProjectService(root, router=router)
    service.initialize(service.propose("星际悬疑"))

    result = service.plan_volume(1, start_chapter=1, end_chapter=3)
    outline = Path(result["outline_file"]).read_text(encoding="utf-8")

    assert "### 第1章 十年后的呼叫" in outline
    assert "### 第3章 被删去的人" in outline
    assert "恶意替换" not in outline
    supplement_payload = json.loads(router.calls[-1][2][0]["text"])
    assert supplement_payload["missing_chapters"] == [2]


def test_plan_does_not_publish_generic_cards_when_model_omits_real_story_content(
    tmp_path: Path,
) -> None:
    class BlankRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            purpose = str(kwargs.get("purpose") or "")
            if purpose == "webnovel:creative:init":
                return super().complete_structured(role, **kwargs)
            self.calls.append((role, purpose, kwargs.get("input")))
            if purpose == "webnovel:creative:plan":
                return SimpleNamespace(
                    parsed={
                        "volume_title": "空白卷",
                        "volume_arc": "林雾寻找真相。",
                        "chapter_cards": [
                            {
                                "chapter": 1,
                                "title": "",
                                "event": "",
                                "character_choice": "",
                                "ending_drive": "",
                            }
                        ],
                    }
                )
            if purpose == "webnovel:creative:plan:supplement":
                return SimpleNamespace(parsed={"chapter_cards": []})
            raise AssertionError(purpose)

    root = tmp_path / "book"
    router = BlankRouter()
    service = CreativeProjectService(root, router=router)
    service.initialize(service.propose("星际悬疑"))

    with pytest.raises(CreativeActionError, match="usable chapter cards"):
        service.plan_volume(1, start_chapter=1, end_chapter=1)

    assert not (root / "大纲" / "第1卷-详细大纲.md").exists()


def test_plan_rechecks_overwrite_under_lock_after_model_generation(tmp_path: Path) -> None:
    root = tmp_path / "book"

    class ConcurrentWriterRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            result = super().complete_structured(role, **kwargs)
            if kwargs.get("purpose") == "webnovel:creative:plan":
                (root / "大纲" / "第1卷-详细大纲.md").write_text(
                    "作者并发写入的大纲", encoding="utf-8"
                )
            return result

    router = ConcurrentWriterRouter()
    service = CreativeProjectService(root, router=router)
    service.initialize(service.propose("星际悬疑"))

    with pytest.raises(CreativeOverwriteError, match="changed during generation"):
        service.plan_volume(1, start_chapter=1, end_chapter=3)

    assert (root / "大纲" / "第1卷-详细大纲.md").read_text(
        encoding="utf-8"
    ) == "作者并发写入的大纲"


def test_init_transaction_rolls_back_partial_files_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "book"
    service = CreativeProjectService(root, router=FakeCreativeRouter())
    proposal = service.propose("星际悬疑")
    real_atomic = CreativeProjectService._atomic_bytes
    failed = False

    def fail_once(path: Path, content: bytes) -> None:
        nonlocal failed
        if path.name == "总纲.md" and not failed:
            failed = True
            raise OSError("injected outline write failure")
        real_atomic(path, content)

    monkeypatch.setattr(
        CreativeProjectService, "_atomic_bytes", staticmethod(fail_once)
    )
    with pytest.raises(OSError, match="injected"):
        service.initialize(proposal)

    state = json.loads((root / ".webnovel" / "state.json").read_text(encoding="utf-8"))
    assert state == {"schema_version": "novel-init-pending/v1"}
    assert not (root / ".webnovel" / "creative_proposal.json").exists()
    assert not (root / "设定集" / "核心设定.md").exists()
    assert not (root / "大纲" / "总纲.md").exists()

    monkeypatch.setattr(
        CreativeProjectService, "_atomic_bytes", staticmethod(real_atomic)
    )
    assert service.initialize(proposal)["status"] == "initialized"


def test_init_rechecks_state_after_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from webnovel_studio import creative_actions

    root = tmp_path / "book"
    service = CreativeProjectService(root, router=FakeCreativeRouter())
    proposal = service.propose("星际悬疑")

    class ReplacingLock:
        def __init__(
            self, project_root: Path, *, timeout: float, require_marker: bool
        ) -> None:
            del timeout, require_marker
            self.project_root = project_root

        def __enter__(self) -> None:
            (self.project_root / ".webnovel" / "state.json").write_text(
                json.dumps({"project": {"title": "并发完成的旧项目"}}, ensure_ascii=False),
                encoding="utf-8",
            )

        def __exit__(self, *args: Any) -> None:
            del args

    monkeypatch.setattr(creative_actions, "ProjectLock", ReplacingLock)

    with pytest.raises(CreativeOverwriteError, match="changed during initialization"):
        service.initialize(proposal)

    assert not (root / "设定集" / "核心设定.md").exists()
    assert not (root / "大纲" / "总纲.md").exists()
    assert json.loads((root / ".webnovel" / "state.json").read_text(encoding="utf-8"))[
        "project"
    ]["title"] == "并发完成的旧项目"


def test_plan_transaction_rolls_back_all_files_and_state_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "book"
    service = CreativeProjectService(root, router=FakeCreativeRouter())
    service.initialize(service.propose("星际悬疑"))
    state_path = root / ".webnovel" / "state.json"
    state_before = state_path.read_bytes()
    real_atomic = CreativeProjectService._atomic_bytes
    failed = False

    def fail_once(path: Path, content: bytes) -> None:
        nonlocal failed
        if "节拍表" in path.name and not failed:
            failed = True
            raise OSError("injected beat write failure")
        real_atomic(path, content)

    monkeypatch.setattr(
        CreativeProjectService, "_atomic_bytes", staticmethod(fail_once)
    )
    with pytest.raises(OSError, match="injected"):
        service.plan_volume(1, start_chapter=1, end_chapter=3)

    assert state_path.read_bytes() == state_before
    assert not (root / "大纲" / "第1卷-详细大纲.md").exists()
    assert not (root / "大纲" / "第1卷-节拍表.md").exists()


def test_plan_aborts_when_author_changes_source_during_generation(tmp_path: Path) -> None:
    root = tmp_path / "book"

    class SourceEditingRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            result = super().complete_structured(role, **kwargs)
            if kwargs.get("purpose") == "webnovel:creative:plan":
                (root / "大纲" / "总纲.md").write_text(
                    "# 作者刚刚修改的新总纲\n", encoding="utf-8"
                )
            return result

    service = CreativeProjectService(root, router=SourceEditingRouter())
    service.initialize(service.propose("星际悬疑"))

    with pytest.raises(CreativeOverwriteError, match="sources changed"):
        service.plan_volume(1, start_chapter=1, end_chapter=3)

    assert not (root / "大纲" / "第1卷-详细大纲.md").exists()


def test_runtime_rejects_symlinked_project_ancestor_before_init_or_plan(
    tmp_path: Path,
) -> None:
    from webnovel_studio import runtime

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    proposal = {
        "title": "不能写到外部",
        "genre": "悬疑",
        "premise": "路径保护测试。",
        "protagonist": "林舟。",
        "core_conflict": "寻找真相。",
        "world": "旧港。",
        "tone": "克制。",
        "opening": "钥匙送达。",
        "long_arc": "追查失踪案。",
    }
    with pytest.raises(CreativeActionError, match="must not contain a symlink"):
        CreativeProjectService(linked / "direct-service", router=FakeCreativeRouter())
    with pytest.raises(CreativeActionError, match="must not contain a symlink"):
        CreativeProjectService(linked, router=FakeCreativeRouter())
    root_link_result = runtime.execute_runtime(
        {
            "action": "project_init",
            "project_root": str(linked),
            "payload": {"confirmed": True, "proposal": proposal},
        },
        llm=object(),
    )
    assert root_link_result["success"] is False
    assert root_link_result["errors"][0]["code"] == "invalid_arguments"
    assert list(outside.iterdir()) == []
    init_result = runtime.execute_runtime(
        {
            "action": "project_init",
            "project_root": str(linked / "new-book"),
            "payload": {"confirmed": True, "proposal": proposal},
        },
        llm=object(),
    )
    assert init_result["success"] is False
    assert init_result["errors"][0]["code"] == "invalid_arguments"
    assert not (outside / "new-book").exists()

    real_project = outside / "existing-book"
    service = CreativeProjectService(real_project, router=FakeCreativeRouter())
    service.initialize(service.propose("旧港悬疑"))
    plan_result = runtime.execute_runtime(
        {
            "action": "project_plan",
            "project_root": str(linked / "existing-book"),
            "volume": 1,
            "payload": {"start_chapter": 1, "end_chapter": 2},
        },
        llm=object(),
    )
    assert plan_result["success"] is False
    assert plan_result["errors"][0]["code"] == "invalid_arguments"
    assert not (real_project / "大纲" / "第1卷-详细大纲.md").exists()


def test_plan_normalizes_multiline_card_fields_without_injecting_chapter_boundaries(
    tmp_path: Path,
) -> None:
    class InjectionRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            if kwargs.get("purpose") != "webnovel:creative:plan":
                return super().complete_structured(role, **kwargs)
            self.calls.append((role, str(kwargs["purpose"]), kwargs.get("input")))
            return SimpleNamespace(
                parsed={
                    "volume_title": "第一卷\n### 第88章 假卷名",
                    "volume_arc": "真实卷弧\n### 第77章 假卷弧",
                    "chapter_cards": [
                        {
                            "chapter": 1,
                            "title": "真标题\n### 第99章 假标题",
                            "event": "真实事件\n### 第98章 假事件",
                            "character_choice": "真实选择\n### 第97章 假选择",
                            "ending_drive": "真实推动\n### 第96章 假推动",
                        },
                        {
                            "chapter": 2,
                            "title": "第二章",
                            "event": "林雾追上信号。",
                            "character_choice": "她选择公开坐标。",
                            "ending_drive": "妹妹的声音从信标传出。",
                        },
                    ],
                }
            )

    root = tmp_path / "injection-book"
    service = CreativeProjectService(root, router=InjectionRouter())
    service.initialize(service.propose("星际悬疑"))
    result = service.plan_volume(1, start_chapter=1, end_chapter=2)

    outline = Path(result["outline_file"]).read_text(encoding="utf-8")
    headings = re.findall(r"^### 第\d+章", outline, flags=re.MULTILINE)
    assert headings == ["### 第1章", "### 第2章"]
    assert "真标题 ### 第99章 假标题" in outline
    chapter_two = NovelContextBuilder(root).build(2)
    assert "林雾追上信号" in chapter_two.chapter_outline
    assert "假事件" not in chapter_two.chapter_outline


def test_plan_rejects_oversized_range_before_model_call(tmp_path: Path) -> None:
    root = tmp_path / "bounded-book"
    router = FakeCreativeRouter()
    service = CreativeProjectService(root, router=router)
    service.initialize(service.propose("星际悬疑"))
    calls_before = len(router.calls)

    with pytest.raises(CreativeActionError, match="at most 500"):
        service.plan_volume(1, start_chapter=1, end_chapter=501)

    assert len(router.calls) == calls_before


def test_plan_rejects_more_returned_cards_than_requested(tmp_path: Path) -> None:
    class TooManyCardsRouter(FakeCreativeRouter):
        def complete_structured(self, role: str, **kwargs: Any) -> Any:
            if kwargs.get("purpose") != "webnovel:creative:plan":
                return super().complete_structured(role, **kwargs)
            card = {
                "title": "标题",
                "event": "事件",
                "character_choice": "选择",
                "ending_drive": "推动",
            }
            return SimpleNamespace(
                parsed={
                    "volume_title": "过量",
                    "volume_arc": "过量",
                    "chapter_cards": [
                        {"chapter": number, **card} for number in (1, 2, 3)
                    ],
                }
            )

    root = tmp_path / "too-many-cards"
    service = CreativeProjectService(root, router=TooManyCardsRouter())
    service.initialize(service.propose("星际悬疑"))

    with pytest.raises(CreativeActionError, match="more chapter cards"):
        service.plan_volume(1, start_chapter=1, end_chapter=2)

    assert not (root / "大纲" / "第1卷-详细大纲.md").exists()

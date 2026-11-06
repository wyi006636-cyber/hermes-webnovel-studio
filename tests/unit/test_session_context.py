from __future__ import annotations

import json
from pathlib import Path

from webnovel_studio.dashboard_service import SessionContextInjector


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    metadata = path / ".webnovel"
    metadata.mkdir()
    (metadata / "state.json").write_text(
        json.dumps(
            {
                "project_info": {"title": "雨夜长歌", "genre": "悬疑"},
                "progress": {"current_chapter": 7, "total_words": 12345},
                "plot_threads": {"open_loops": [{"id": "loop-1"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def test_session_context_requires_explicit_runtime_binding_and_injects_once(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "novel")
    injector = SessionContextInjector()

    assert injector.context(session_id="session-1") is None
    assert injector.bind(session_id="session-1", project_root=project) is True
    first = injector.context(session_id="session-1")
    second = injector.context(session_id="session-1")
    other = injector.context(session_id="session-2")

    assert first is not None
    assert first["context"].startswith("[WEBNOVEL PROJECT]")
    assert "雨夜长歌" in first["context"]
    assert "第 7 章" in first["context"]
    assert len(first["context"]) < 300
    assert second is None
    assert other is None


def test_registered_hook_uses_official_session_only_signature_after_binding(
    tmp_path: Path,
) -> None:
    from webnovel_studio import _SESSION_CONTEXT, _pre_llm_call_session_context

    project = make_project(tmp_path / "hook-project")
    session_id = f"session-hook-{tmp_path.name}"
    assert _pre_llm_call_session_context(
        session_id=session_id,
        user_message="普通对话",
        conversation_history=[],
    ) is None
    assert _SESSION_CONTEXT.bind(session_id=session_id, project_root=project) is True
    first = _pre_llm_call_session_context(
        session_id=session_id,
        user_message="继续写作",
        conversation_history=[],
    )
    second = _pre_llm_call_session_context(
        session_id=session_id,
        user_message="继续",
        conversation_history=[],
    )

    assert first is not None
    assert second is None


def test_corrupt_project_state_fails_silently_without_marking_session(
    tmp_path: Path,
) -> None:
    project = tmp_path / "corrupt"
    metadata = project / ".webnovel"
    metadata.mkdir(parents=True)
    (metadata / "state.json").write_text(
        '{"project":1,"project":2}', encoding="utf-8"
    )
    injector = SessionContextInjector()

    assert injector.bind(session_id="session-1", project_root=project) is True
    assert injector.context(session_id="session-1") is None
    (metadata / "state.json").write_text(
        '{"project_info":{"title":"修复后"},"progress":{"current_chapter":1,"total_words":0}}\n',
        encoding="utf-8",
    )
    assert injector.context(session_id="session-1") is not None


def test_symlink_project_binding_is_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path / "real")
    linked = tmp_path / "linked"
    linked.symlink_to(project, target_is_directory=True)
    injector = SessionContextInjector()

    assert injector.bind(session_id="session-1", project_root=linked) is False
    assert injector.context(session_id="session-1") is None

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any

import pytest

from webnovel_studio.project_lock import ProjectLock, ProjectLockError, holds_project_lock


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    metadata = path / ".webnovel"
    metadata.mkdir()
    (metadata / "state.json").write_text("{}\n", encoding="utf-8")
    return path.resolve()


def _hold_lock(project: str, ready: Any, release: Any) -> None:
    with ProjectLock(project, timeout=2):
        ready.set()
        release.wait(10)


def _hold_until_killed(project: str, ready: Any) -> None:
    with ProjectLock(project, timeout=2):
        ready.set()
        while True:
            time.sleep(10)


def _context() -> Any:
    return mp.get_context("spawn")


def symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")


def test_lock_path_is_the_single_project_run_lock_and_context_manager_is_idempotent(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "小说")
    lock = ProjectLock(project, timeout=0.1)

    assert lock.lock_path == project / ".webnovel" / "run.lock"
    assert lock.is_acquired is False
    assert lock.acquire() is lock
    assert lock.acquire() is lock
    assert lock.is_acquired is True
    lock.release()
    lock.release()
    assert lock.is_acquired is False

    with lock as acquired:
        assert acquired is lock
        assert lock.is_acquired is True
    assert lock.is_acquired is False


def test_project_is_validated_before_lock_file_is_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()

    with pytest.raises(ProjectLockError, match="project.*does not exist"):
        ProjectLock(missing)
    with pytest.raises(ProjectLockError, match=r"missing .*\.webnovel/state\.json"):
        ProjectLock(unmarked)

    assert not (unmarked / ".webnovel" / "run.lock").exists()


def test_nested_context_does_not_release_outer_context_early(tmp_path: Path) -> None:
    project = make_project(tmp_path / "nested")
    lock = ProjectLock(project, timeout=0.1)

    with lock:
        with lock:
            assert lock.is_acquired is True
        assert lock.is_acquired is True

    assert lock.is_acquired is False

    lock.acquire()
    with lock:
        assert lock.is_acquired is True
    assert lock.is_acquired is True
    lock.release()


def test_release_failure_preserves_ownership_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path / "release-retry")
    lock = ProjectLock(project, timeout=0.1).acquire()
    original_release = lock._lock.release

    def fail_release(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("simulated release failure")

    monkeypatch.setattr(lock._lock, "release", fail_release)
    with pytest.raises(ProjectLockError, match="failed to release.*simulated"):
        lock.release()
    assert lock.is_acquired is True

    monkeypatch.setattr(lock._lock, "release", original_release)
    lock.release()
    assert lock.is_acquired is False


def test_real_process_contention_times_out_with_project_and_lock_path(tmp_path: Path) -> None:
    project = make_project(tmp_path / "竞争项目")
    ctx = _context()
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(str(project), ready, release))
    process.start()
    try:
        assert ready.wait(5), "child did not acquire project lock"
        contender = ProjectLock(project, timeout=0.15)
        with pytest.raises(ProjectLockError) as exc_info:
            contender.acquire()
        message = str(exc_info.value)
        assert str(project) in message
        assert str(project / ".webnovel" / "run.lock") in message
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_lock_is_reacquired_after_abnormal_process_exit(tmp_path: Path) -> None:
    project = make_project(tmp_path / "崩溃恢复")
    ctx = _context()
    ready = ctx.Event()
    process = ctx.Process(target=_hold_until_killed, args=(str(project), ready))
    process.start()
    try:
        assert ready.wait(5), "child did not acquire project lock"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
    assert process.exitcode not in {None, 0}

    with ProjectLock(project, timeout=2) as recovered:
        assert recovered.is_acquired


def test_stale_lock_file_does_not_block_and_is_not_deleted_on_release(tmp_path: Path) -> None:
    project = make_project(tmp_path / "stale")
    lock_path = project / ".webnovel" / "run.lock"
    lock_path.write_text("stale pid=999999\n", encoding="utf-8")

    with ProjectLock(project, timeout=0.2):
        assert lock_path.exists()

    assert lock_path.exists()
    assert not any(path.name != "run.lock" and path.is_file() for path in lock_path.parent.iterdir() if path.name != "state.json")


def test_marker_removed_after_construction_is_revalidated_before_acquire(tmp_path: Path) -> None:
    project = make_project(tmp_path / "changed")
    lock = ProjectLock(project)
    os.unlink(project / ".webnovel" / "state.json")

    with pytest.raises(ProjectLockError, match=r"missing .*\.webnovel/state\.json"):
        lock.acquire()


def test_project_metadata_directory_symlink_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-metadata"
    outside.mkdir()
    (outside / "state.json").write_text("{}\n", encoding="utf-8")
    symlink_or_skip(outside, project / ".webnovel", directory=True)

    with pytest.raises(ProjectLockError, match=r"\.webnovel.*symlink"):
        ProjectLock(project)


def test_project_state_symlink_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    metadata = project / ".webnovel"
    metadata.mkdir(parents=True)
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}\n", encoding="utf-8")
    symlink_or_skip(outside, metadata / "state.json")

    with pytest.raises(ProjectLockError, match=r"state\.json.*symlink"):
        ProjectLock(project)


def test_optional_marker_mode_rejects_state_path_that_is_not_a_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".webnovel" / "state.json").mkdir(parents=True)

    with pytest.raises(ProjectLockError, match="state.json must be a regular file"):
        ProjectLock(project, require_marker=False)


def test_existing_run_lock_symlink_is_rejected_before_acquire(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    lock = ProjectLock(project)
    outside = tmp_path / "outside.lock"
    outside.write_text("do not lock through me\n", encoding="utf-8")
    symlink_or_skip(outside, lock.lock_path)

    with pytest.raises(ProjectLockError, match=r"run\.lock.*symlink"):
        lock.acquire()
    assert outside.read_text(encoding="utf-8") == "do not lock through me\n"


def test_live_lock_capability_rejects_unacquired_and_subclass_spoofs(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project-capability")

    class ForgedProjectLock(ProjectLock):
        @property
        def is_acquired(self) -> bool:
            return True

    unacquired = ProjectLock(project)
    forged = ForgedProjectLock(project)
    assert holds_project_lock(unacquired, project) is False
    assert holds_project_lock(forged, project) is False
    with unacquired:
        assert holds_project_lock(unacquired, project) is True
    assert holds_project_lock(unacquired, project) is False

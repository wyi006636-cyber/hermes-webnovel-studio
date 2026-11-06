"""Per-project cross-process single-writer lock.

The lock deliberately uses the one project-scoped ``.webnovel/run.lock`` file.
It does not add a second business lock or remove the lock file on release: the
operating-system lock, rather than file existence or file contents, determines
ownership.
"""

from __future__ import annotations

import os
import time
import weakref
from pathlib import Path
from types import TracebackType

try:
    from filelock import FileLock as _FileLock  # type: ignore[assignment]
    from filelock import Timeout as _Timeout  # type: ignore[assignment]
except ImportError:  # Hermes' isolated plugin interpreter may not ship filelock.
    class _Timeout(Exception):
        """Compatibility timeout used by the stdlib lock fallback."""

    class _FileLock:
        """Small process lock fallback with the subset ProjectLock needs."""

        def __init__(self, path: str, *, timeout: float = 10.0) -> None:
            self.path = path
            self.timeout = float(timeout)
            self._handle: object | None = None

        @property
        def is_locked(self) -> bool:
            return self._handle is not None

        def acquire(self, *, timeout: float | None = None) -> None:
            effective = self.timeout if timeout is None else float(timeout)
            deadline = time.monotonic() + max(0.0, effective)
            handle = open(self.path, "a+b")
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        if handle.read(1) == b"":
                            handle.write(b"0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._handle = handle
                    return
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise _Timeout(self.path)
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        def release(self) -> None:
            handle = self._handle
            if handle is None:
                return
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)  # type: ignore[attr-defined]
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                handle.close()  # type: ignore[attr-defined]
                self._handle = None


PROJECT_MARKER = Path(".webnovel") / "state.json"
PROJECT_LOCK = Path(".webnovel") / "run.lock"
_ACTIVE_LOCKS: weakref.WeakSet[object] = weakref.WeakSet()


class ProjectLockError(RuntimeError):
    """Raised when a project lock cannot be validated or acquired."""


def _canonical_project_root(
    project_root: str | os.PathLike[str], *, require_marker: bool = True
) -> Path:
    requested = Path(project_root).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if any(component.is_symlink() for component in (requested, *requested.parents)):
        raise ProjectLockError("project path must not contain a symlink")
    root = requested.resolve(strict=False)
    if not root.exists():
        raise ProjectLockError(f"project does not exist: {root}")
    if not root.is_dir():
        raise ProjectLockError(f"project is not a directory: {root}")

    metadata = root / ".webnovel"
    marker = root / PROJECT_MARKER
    if metadata.is_symlink():
        raise ProjectLockError(f"project .webnovel must not be a symlink: {metadata}")
    if marker.is_symlink():
        raise ProjectLockError(f"project state.json must not be a symlink: {marker}")
    if marker.exists() and not marker.is_file():
        raise ProjectLockError(
            f"project state.json must be a regular file when present: {marker}"
        )
    if require_marker and not marker.is_file():
        raise ProjectLockError(
            f"project is missing required .webnovel/state.json: {root}"
        )
    paths = [(".webnovel", metadata)]
    if marker.exists() or require_marker:
        paths.append(("state.json", marker))
    for label, path in paths:
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ProjectLockError(
                f"project {label} resolves outside project root: {path}"
            ) from exc
    return root


def _validate_lock_path(project_root: Path, lock_path: Path) -> None:
    if lock_path.is_symlink():
        raise ProjectLockError(f"project run.lock must not be a symlink: {lock_path}")
    try:
        lock_path.resolve(strict=False).relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise ProjectLockError(
            f"project run.lock resolves outside project root: {lock_path}"
        ) from exc


class ProjectLock:
    """A reusable, idempotent context manager for one project's writer lock.

    Calling :meth:`acquire` on an already acquired instance is a no-op; calling
    :meth:`release` on an unacquired instance is also a no-op.  The lock file is
    intentionally retained after release so a stale file can never be confused
    with an active operating-system lock.
    """

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        timeout: float = 10.0,
        require_marker: bool = True,
    ) -> None:
        self.require_marker = require_marker
        self.project_root = _canonical_project_root(
            project_root, require_marker=require_marker
        )
        self.lock_path = self.project_root / PROJECT_LOCK
        self.timeout = float(timeout)
        self._lock = _FileLock(str(self.lock_path), timeout=self.timeout)
        self._acquired = False
        self._context_ownership: list[bool] = []

    @property
    def is_acquired(self) -> bool:
        """Whether this instance currently owns the operating-system lock."""

        return self._acquired and self._lock.is_locked

    def acquire(self, *, timeout: float | None = None) -> ProjectLock:
        """Acquire the project writer lock or raise a contextual domain error."""

        if self.is_acquired:
            return self

        # Validate immediately before any lock-file creation or protected write.
        _canonical_project_root(
            self.project_root, require_marker=self.require_marker
        )
        _validate_lock_path(self.project_root, self.lock_path)
        effective_timeout = self.timeout if timeout is None else float(timeout)
        try:
            self._lock.acquire(timeout=effective_timeout)
        except _Timeout as exc:
            raise ProjectLockError(
                "timed out acquiring project writer lock "
                f"(project={self.project_root}, lock={self.lock_path}, "
                f"timeout={effective_timeout})"
            ) from exc
        except OSError as exc:
            raise ProjectLockError(
                "failed to acquire project writer lock "
                f"(project={self.project_root}, lock={self.lock_path}): {exc}"
            ) from exc
        self._acquired = True
        _ACTIVE_LOCKS.add(self)
        return self

    def release(self) -> None:
        """Release this instance's lock; repeated releases are harmless."""

        if not self._acquired:
            return
        try:
            self._lock.release()
        except OSError as exc:
            raise ProjectLockError(
                "failed to release project writer lock "
                f"(project={self.project_root}, lock={self.lock_path}): {exc}"
            ) from exc
        self._acquired = False
        _ACTIVE_LOCKS.discard(self)

    def __enter__(self) -> ProjectLock:
        already_acquired = self.is_acquired
        acquired = self.acquire()
        self._context_ownership.append(not already_acquired)
        return acquired

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._context_ownership.pop():
            self.release()


def holds_project_lock(value: object, project_root: str | os.PathLike[str]) -> bool:
    """Return true only for this module's live, OS-acquired lock capability."""

    if type(value) is not ProjectLock or value not in _ACTIVE_LOCKS:
        return False
    lock = value
    return lock.is_acquired and lock.project_root == _canonical_project_root(
        project_root, require_marker=lock.require_marker
    )


__all__ = [
    "PROJECT_LOCK",
    "PROJECT_MARKER",
    "ProjectLock",
    "ProjectLockError",
    "holds_project_lock",
]

"""Read-only dashboard process management and one-shot project session context."""

from __future__ import annotations

import atexit
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .upstream_bridge import UpstreamBridge


class DashboardError(RuntimeError):
    """Raised when the local dashboard cannot be started safely."""


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(
            handle,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def _free_loopback_port(requested: int | None) -> int:
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise DashboardError("dashboard port must be an integer")
        if requested < 1 or requested > 65535:
            raise DashboardError("dashboard port must be between 1 and 65535")
    candidates = (requested, None) if requested is not None else (None,)
    for candidate in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", 0 if candidate is None else candidate))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise DashboardError("no loopback port is available")


@dataclass
class DashboardHandle:
    """A verified local dashboard process."""

    process: subprocess.Popen[bytes]
    host: str
    port: int
    url: str
    health: dict[str, Any]
    preflight: dict[str, Any]

    def stop(self, *, timeout: float = 5.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)


class DashboardService:
    """Launch the vendored GET-only dashboard on an available loopback port."""

    def __init__(self, project_root: str | Path, *, startup_timeout: float = 8.0) -> None:
        self.bridge = UpstreamBridge(project_root=project_root)
        self.project_root = self.bridge.project_root
        self.startup_timeout = float(startup_timeout)
        if self.startup_timeout <= 0:
            raise DashboardError("startup_timeout must be positive")

    @staticmethod
    def _read_health(url: str) -> dict[str, Any]:
        with urllib.request.urlopen(
            f"{url}/api/story-runtime/health", timeout=1
        ) as response:
            if response.status != 200:
                raise DashboardError(
                    f"dashboard health returned HTTP {response.status}"
                )
            raw = response.read()
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
        if not isinstance(value, dict):
            raise DashboardError("dashboard health response is not an object")
        return value

    def start(
        self, *, port: int | None = None, no_browser: bool = True
    ) -> DashboardHandle:
        preflight = self.bridge.run("preflight", timeout=30).as_dict()
        if not preflight["ok"]:
            raise DashboardError("upstream preflight failed before dashboard start")

        selected_port = _free_loopback_port(port)
        host = "127.0.0.1"
        url = f"http://{host}:{selected_port}"
        command = [
            str(self.bridge.python_executable),
            "-m",
            "dashboard.server",
            "--project-root",
            str(self.project_root),
            "--host",
            host,
            "--port",
            str(selected_port),
        ]
        if no_browser:
            command.append("--no-browser")
        environment = self.bridge.child_env
        process = subprocess.Popen(
            command,
            cwd=str(self.bridge.upstream_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise DashboardError(
                    f"dashboard process exited before readiness with code {process.returncode}"
                )
            try:
                health = self._read_health(url)
                return DashboardHandle(
                    process=process,
                    host=host,
                    port=selected_port,
                    url=url,
                    health=health,
                    preflight=preflight,
                )
            except (DashboardError, OSError, ValueError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(0.05)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        raise DashboardError(f"dashboard did not become ready: {last_error}")


class DashboardRegistry:
    """Own dashboard handles so runtime-started processes remain controllable."""

    def __init__(self) -> None:
        self._handles: dict[Path, DashboardHandle] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(project_root: str | Path) -> Path:
        return Path(project_root).expanduser().resolve(strict=True)

    def inspect(self, project_root: str | Path) -> DashboardHandle | None:
        key = self._key(project_root)
        with self._lock:
            handle = self._handles.get(key)
            if handle is not None and handle.process.poll() is not None:
                self._handles.pop(key, None)
                return None
            return handle

    def start(
        self,
        project_root: str | Path,
        *,
        port: int | None = None,
    ) -> DashboardHandle:
        key = self._key(project_root)
        with self._lock:
            existing = self._handles.get(key)
            if existing is not None and existing.process.poll() is None:
                return existing
            handle = DashboardService(key).start(port=port, no_browser=True)
            self._handles[key] = handle
            return handle

    def stop(self, project_root: str | Path) -> bool:
        key = self._key(project_root)
        with self._lock:
            handle = self._handles.pop(key, None)
        if handle is None:
            return False
        handle.stop()
        return True

    def stop_all(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle.stop()


DASHBOARD_REGISTRY = DashboardRegistry()
atexit.register(DASHBOARD_REGISTRY.stop_all)


class SessionContextInjector:
    """Inject one status hint after a runtime call binds a session to a project."""

    def __init__(self) -> None:
        self._injected_sessions: set[str] = set()
        self._session_projects: dict[str, Path] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validated_project_root(project_root: str | Path) -> Path | None:
        raw = Path(project_root).expanduser()
        if raw.is_symlink():
            return None
        try:
            root = raw.resolve(strict=True)
        except OSError:
            return None
        metadata = root / ".webnovel"
        marker = metadata / "state.json"
        if (
            metadata.is_symlink()
            or marker.is_symlink()
            or not root.is_dir()
            or not marker.is_file()
        ):
            return None
        return root

    def bind(self, *, session_id: str, project_root: str | Path) -> bool:
        if not isinstance(session_id, str) or not session_id:
            return False
        root = self._validated_project_root(project_root)
        if root is None:
            return False
        with self._lock:
            self._session_projects[session_id] = root
        return True

    def context(
        self,
        *,
        session_id: str,
        project_root: str | Path | None = None,
    ) -> dict[str, str] | None:
        if not isinstance(session_id, str) or not session_id:
            return None
        if project_root is not None:
            self.bind(session_id=session_id, project_root=project_root)
        with self._lock:
            if session_id in self._injected_sessions:
                return None
            root = self._session_projects.get(session_id)
        if root is None:
            return None
        try:
            state = _load_json(root / ".webnovel" / "state.json")
            project = state.get("project_info", state.get("project", {}))
            progress = state.get("progress", {})
            if not isinstance(project, dict) or not isinstance(progress, dict):
                return None
            title = project.get("title")
            chapter = progress.get("current_chapter", 0)
            words = progress.get("total_words", 0)
            if not isinstance(title, str) or not title.strip():
                return None
            if isinstance(chapter, bool) or not isinstance(chapter, int):
                return None
            if isinstance(words, bool) or not isinstance(words, int):
                return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            if session_id in self._injected_sessions:
                return None
            if self._session_projects.get(session_id) != root:
                return None
            self._injected_sessions.add(session_id)
        return {
            "context": (
                f"[WEBNOVEL PROJECT] 《{title.strip()}》；当前第 {chapter} 章；"
                f"累计 {words} 字。写入请走 webnovel_runtime。"
            )
        }


__all__ = [
    "DASHBOARD_REGISTRY",
    "DashboardError",
    "DashboardHandle",
    "DashboardRegistry",
    "DashboardService",
    "SessionContextInjector",
]

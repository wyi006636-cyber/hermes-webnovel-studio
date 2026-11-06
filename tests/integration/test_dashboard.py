from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from webnovel_studio.dashboard_service import DashboardService
from webnovel_studio.runtime import execute_runtime


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    metadata = path / ".webnovel"
    metadata.mkdir()
    (metadata / "state.json").write_text(
        json.dumps(
            {
                "project_info": {"title": "雨夜长歌", "genre": "悬疑"},
                "progress": {"current_chapter": 0, "total_words": 0},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def test_dashboard_starts_on_loopback_is_get_only_and_stops_cleanly(tmp_path: Path) -> None:
    project = make_project(tmp_path / "novel")
    before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    service = DashboardService(project, startup_timeout=10)

    handle = service.start(port=None, no_browser=True)
    try:
        assert handle.host == "127.0.0.1"
        assert handle.port > 0
        assert handle.url == f"http://127.0.0.1:{handle.port}"
        with urllib.request.urlopen(
            f"{handle.url}/api/story-runtime/health", timeout=3
        ) as response:
            assert response.status == 200
            health = json.load(response)
        assert isinstance(health, dict)
        assert handle.preflight["ok"] is True
        request = urllib.request.Request(
            f"{handle.url}/api/project/info", data=b"{}", method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("dashboard accepted a non-GET request")
    finally:
        handle.stop(timeout=5)

    assert handle.process.poll() is not None
    after = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_runtime_dashboard_start_and_stop_uses_verified_live_handle(tmp_path: Path) -> None:
    project = make_project(tmp_path / "runtime-novel")
    started = execute_runtime(
        {
            "action": "dashboard_start",
            "project_root": str(project),
            "payload": {"operation": "start"},
        }
    )
    try:
        assert started["success"] is True, started
        assert started["status"] == "running"
        assert started["outputs"]["launched"] is True
        with urllib.request.urlopen(
            f"{started['outputs']['url']}/api/story-runtime/health",
            timeout=2,
        ) as response:
            assert response.status == 200
    finally:
        stopped = execute_runtime(
            {
                "action": "dashboard_start",
                "project_root": str(project),
                "payload": {"operation": "stop"},
            }
        )
    assert stopped["success"] is True
    assert stopped["status"] == "stopped"

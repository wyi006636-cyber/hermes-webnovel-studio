from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from webnovel_studio.upstream_bridge import (
    BridgeError,
    UpstreamBridge,
    build_subprocess_env,
    resolve_adapter_root,
    resolve_project_root,
    resolve_python_executable,
    resolve_upstream_root,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UPSTREAM_ROOT = (
    REPOSITORY_ROOT / "upstream" / "webnovel-writer" / "webnovel-writer"
)


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    marker = path / ".webnovel" / "state.json"
    marker.parent.mkdir()
    marker.write_text("{}\n", encoding="utf-8")
    return path.resolve()


def make_upstream(path: Path) -> Path:
    script = path / "scripts" / "webnovel.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fixture')\n", encoding="utf-8")
    return path.resolve()


def test_adapter_root_defaults_to_package_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBNOVEL_HERMES_ROOT", raising=False)

    assert resolve_adapter_root() == (REPOSITORY_ROOT / "webnovel_studio").resolve()


def test_adapter_root_accepts_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "adapter 安装"
    override.mkdir()
    monkeypatch.setenv("WEBNOVEL_HERMES_ROOT", str(override))

    assert resolve_adapter_root() == override.resolve()


def test_adapter_root_rejects_invalid_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-adapter"
    monkeypatch.setenv("WEBNOVEL_HERMES_ROOT", str(missing))

    with pytest.raises(BridgeError, match="adapter root.*does not exist"):
        resolve_adapter_root()


def test_upstream_root_defaults_to_vendored_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBNOVEL_UPSTREAM_ROOT", raising=False)

    assert resolve_upstream_root() == DEFAULT_UPSTREAM_ROOT.resolve()


def test_upstream_root_uses_plugin_local_vendor_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webnovel_studio.upstream_bridge as bridge_module

    plugin = tmp_path / "plugins" / "webnovel-writer"
    installed = make_upstream(plugin / "_vendor" / "webnovel-writer")
    fake_module = plugin / "upstream_bridge.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# installed layout\n", encoding="utf-8")
    monkeypatch.delenv("WEBNOVEL_UPSTREAM_ROOT", raising=False)
    monkeypatch.setattr(bridge_module, "__file__", str(fake_module))

    assert bridge_module.resolve_upstream_root() == installed


def test_upstream_root_precedence_is_explicit_then_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = make_upstream(tmp_path / "explicit")
    environment = make_upstream(tmp_path / "environment")
    monkeypatch.setenv("WEBNOVEL_UPSTREAM_ROOT", str(environment))

    assert resolve_upstream_root(explicit) == explicit
    assert resolve_upstream_root() == environment


def test_upstream_root_rejects_missing_unified_cli(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()

    with pytest.raises(BridgeError, match=r"scripts/webnovel\.py"):
        resolve_upstream_root(upstream)


def test_project_root_precedence_is_explicit_then_environment_then_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = make_project(tmp_path / "explicit")
    environment = make_project(tmp_path / "environment")
    cwd_project = make_project(tmp_path / "cwd-project")
    monkeypatch.setenv("WEBNOVEL_PROJECT_ROOT", str(environment))

    assert resolve_project_root(explicit, cwd=cwd_project) == explicit
    assert resolve_project_root(cwd=cwd_project) == environment
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT")
    assert resolve_project_root(cwd=cwd_project) == cwd_project


def test_project_root_is_discovered_from_nested_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    project = make_project(tmp_path / "书项目")
    nested = project / "正文" / "第一卷"
    nested.mkdir(parents=True)

    assert resolve_project_root(cwd=nested) == project


def test_project_root_supports_relative_explicit_path_and_returns_canonical_absolute_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = make_project(workspace / "小说")

    resolved = resolve_project_root("小说", cwd=workspace, environ={})

    assert resolved == project
    assert resolved.is_absolute()


def test_project_root_does_not_guess_an_unrelated_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEBNOVEL_PROJECT_ROOT", raising=False)
    make_project(tmp_path / "other-project")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(BridgeError, match="no .webnovel/state.json"):
        resolve_project_root(cwd=outside)


@pytest.mark.parametrize("kind", ["missing", "file", "unmarked"])
def test_explicit_project_root_is_validated_before_spawn(
    tmp_path: Path, kind: str
) -> None:
    candidate = tmp_path / kind
    if kind == "file":
        candidate.write_text("not a directory", encoding="utf-8")
        expected = "not a directory"
    elif kind == "unmarked":
        candidate.mkdir()
        expected = r"missing .webnovel/state\.json"
    else:
        expected = "does not exist"

    with pytest.raises(BridgeError, match=expected):
        resolve_project_root(candidate, environ={})


def test_python_executable_defaults_to_current_interpreter() -> None:
    assert resolve_python_executable() == Path(sys.executable)


def _make_venv_python(root: Path) -> Path:
    executable = root / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_python_executable_prefers_project_virtualenv(tmp_path: Path) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    expected = _make_venv_python(project)

    assert resolve_python_executable(
        project_root=project, adapter_root=adapter
    ) == expected


def test_python_executable_uses_installer_managed_source_virtualenv(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    adapter = tmp_path / "plugins" / "webnovel-writer"
    adapter.mkdir(parents=True)
    source = tmp_path / "source" / "webnovel_studio"
    source.mkdir(parents=True)
    expected = _make_venv_python(source.parent)
    (adapter / ".webnovel-hermes-install.json").write_text(
        json.dumps(
            {
                "schema": "webnovel-hermes-install/v1",
                "source": str(source),
                "runtime_python": str(expected),
            }
        ),
        encoding="utf-8",
    )

    assert resolve_python_executable(
        project_root=project, adapter_root=adapter
    ) == expected


def test_python_executable_rejects_tampered_installer_runtime(
    tmp_path: Path,
) -> None:
    project = tmp_path / "novel"
    project.mkdir()
    adapter = tmp_path / "plugins" / "webnovel-writer"
    adapter.mkdir(parents=True)
    source = tmp_path / "source" / "webnovel_studio"
    source.mkdir(parents=True)
    unexpected = _make_venv_python(tmp_path / "other")
    (adapter / ".webnovel-hermes-install.json").write_text(
        json.dumps(
            {
                "schema": "webnovel-hermes-install/v1",
                "source": str(source),
                "runtime_python": str(unexpected),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BridgeError, match="unexpected interpreter"):
        resolve_python_executable(project_root=project, adapter_root=adapter)


def test_python_executable_accepts_injected_path_and_rejects_missing(
    tmp_path: Path,
) -> None:
    injected = tmp_path / "python-fixture"
    injected.write_text("", encoding="utf-8")
    injected.chmod(0o755)

    assert resolve_python_executable(injected) == injected.resolve()
    with pytest.raises(BridgeError, match="Python executable.*does not exist"):
        resolve_python_executable(tmp_path / "missing-python")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits do not apply")
def test_python_executable_rejects_non_executable_file(tmp_path: Path) -> None:
    injected = tmp_path / "python-fixture"
    injected.write_text("", encoding="utf-8")
    injected.chmod(0o644)

    with pytest.raises(BridgeError, match="Python executable.*not executable"):
        resolve_python_executable(injected)


def test_environment_bridge_maps_roots_without_polluting_global_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "中文项目")
    hermes_home = tmp_path / "isolated-hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    before = os.environ.copy()

    child_env = build_subprocess_env(
        adapter_root=adapter,
        upstream_root=upstream,
        project_root=project,
        base_env={**os.environ, "PYTHONIOENCODING": "latin-1"},
    )

    assert child_env["WEBNOVEL_HERMES_ROOT"] == str(adapter.resolve())
    assert child_env["WEBNOVEL_UPSTREAM_ROOT"] == str(upstream)
    assert child_env["WEBNOVEL_PROJECT_ROOT"] == str(project)
    assert child_env["CLAUDE_PLUGIN_ROOT"] == str(upstream)
    assert child_env["CLAUDE_PROJECT_DIR"] == str(project)
    assert child_env["WEBNOVEL_CLAUDE_HOME"] == str(
        (hermes_home / "webnovel-writer").resolve()
    )
    assert child_env["PYTHONIOENCODING"] == "utf-8"
    assert os.environ == before


def test_environment_bridge_uses_dot_hermes_under_home_when_hermes_home_is_absent(
    tmp_path: Path,
) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")

    child_env = build_subprocess_env(
        adapter_root=adapter,
        upstream_root=upstream,
        project_root=project,
        base_env={"HOME": str(tmp_path / "home")},
    )

    assert child_env["WEBNOVEL_CLAUDE_HOME"] == str(
        (tmp_path / "home" / ".hermes" / "webnovel-writer").resolve()
    )


def test_run_uses_argv_without_shell_and_preserves_nonzero_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    recorded: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded["command"] = command
        recorded.update(kwargs)
        return subprocess.CompletedProcess(command, 7, "json output", "diagnostic")

    monkeypatch.setattr("webnovel_studio.upstream_bridge.subprocess.run", fake_run)
    bridge = UpstreamBridge(
        project_root=project,
        upstream_root=upstream,
        adapter_root=adapter,
        python_executable=python,
        environ={"HOME": str(tmp_path)},
    )

    result = bridge.run("doctor", "--format", "json", timeout=12.5)

    expected_command = [
        str(python.resolve()),
        str(upstream / "scripts" / "webnovel.py"),
        "--project-root",
        str(project),
        "doctor",
        "--format",
        "json",
    ]
    assert recorded["command"] == expected_command
    assert recorded["cwd"] == project
    assert recorded["shell"] is False
    assert recorded["capture_output"] is True
    assert recorded["text"] is True
    assert recorded["encoding"] == "utf-8"
    assert recorded["check"] is False
    assert recorded["timeout"] == 12.5
    assert recorded["env"]["PYTHONIOENCODING"] == "utf-8"
    assert result.command == tuple(expected_command)
    assert result.returncode == 7
    assert result.stdout == "json output"
    assert result.stderr == "diagnostic"
    assert result.ok is False


def test_command_treats_single_string_argument_as_one_argv_token(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")
    bridge = UpstreamBridge(project_root=project, upstream_root=upstream)

    command = bridge.command("doctor", "--deep")

    assert command[-2:] == ["doctor", "--deep"]


@pytest.mark.parametrize(
    "spawn_error",
    [PermissionError("permission denied"), FileNotFoundError("missing executable")],
)
def test_run_wraps_spawn_oserror_with_command_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawn_error: OSError,
) -> None:
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")
    bridge = UpstreamBridge(project_root=project, upstream_root=upstream)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del command, kwargs
        raise spawn_error

    monkeypatch.setattr("webnovel_studio.upstream_bridge.subprocess.run", fake_run)

    with pytest.raises(BridgeError) as exc_info:
        bridge.run("doctor", "--deep")

    message = str(exc_info.value)
    assert f"python={bridge.python_executable}" in message
    assert f"cli={bridge.cli_path}" in message
    assert repr(bridge.command("doctor", "--deep")) in message


def test_run_propagates_timeout_from_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")
    command_seen: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        command_seen.extend(command)
        raise subprocess.TimeoutExpired(command, 0.01, output="partial", stderr="slow")

    monkeypatch.setattr("webnovel_studio.upstream_bridge.subprocess.run", fake_run)
    bridge = UpstreamBridge(project_root=project, upstream_root=upstream)

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        bridge.where(timeout=0.01)

    assert command_seen
    assert exc_info.value.timeout == 0.01


def test_convenience_methods_select_expected_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = make_upstream(tmp_path / "upstream")
    project = make_project(tmp_path / "project")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr("webnovel_studio.upstream_bridge.subprocess.run", fake_run)
    bridge = UpstreamBridge(project_root=project, upstream_root=upstream)

    bridge.where()
    bridge.preflight(output_format="json")
    bridge.doctor(output_format="json", chapter=3, deep=True)

    assert [command[4] for command in commands] == ["where", "preflight", "doctor"]
    assert commands[1][-2:] == ["--format", "json"]
    assert commands[2][-5:] == ["--format", "json", "--chapter", "3", "--deep"]


def test_real_upstream_where_preflight_and_doctor_on_minimal_chinese_project(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "真实中文小说")
    bridge = UpstreamBridge(project_root=project)

    where = bridge.where(timeout=30)
    preflight = bridge.preflight(output_format="json", timeout=30)
    doctor = bridge.doctor(output_format="json", timeout=30)

    assert where.returncode == 0, where.stderr
    assert Path(where.stdout.strip()) == project

    preflight_payload = json.loads(preflight.stdout)
    assert preflight.returncode == 0, preflight.stderr
    assert preflight_payload["ok"] is True
    assert Path(preflight_payload["project_root"]) == project

    doctor_payload = json.loads(doctor.stdout)
    assert doctor.returncode in {0, 1}
    assert isinstance(doctor_payload["ok"], bool)
    assert doctor.ok is (doctor.returncode == 0)

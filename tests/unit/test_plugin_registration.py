from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import webnovel_studio


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ACTIONS = [
    "project_status",
    "project_init",
    "project_plan",
    "write_chapter",
    "review_range",
    "query",
    "doctor",
    "dashboard_start",
    "run_resume",
    "run_abort",
]

class RecordingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.llm: Any = None

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_hook(self, hook_name: str, callback: Any) -> None:
        self.hooks.append((hook_name, callback))

    def register_skill(
        self, name: str, path: Path, description: str = ""
    ) -> None:
        self.skills.append(
            {"name": name, "path": path, "description": description}
        )

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands.append(kwargs)


def registered_context() -> RecordingContext:
    context = RecordingContext()
    webnovel_studio.register(context)
    return context


def test_manifest_declares_plugin_surface() -> None:
    manifest_path = REPOSITORY_ROOT / "webnovel_studio" / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"] == "webnovel-writer"
    assert manifest["provides_tools"] == ["webnovel_runtime"]
    assert manifest["provides_hooks"] == ["pre_tool_call", "pre_llm_call"]


def test_registers_tool_skills_hooks_and_cli_command() -> None:
    context = registered_context()

    assert [tool["name"] for tool in context.tools] == ["webnovel_runtime"]
    assert context.tools[0]["toolset"] == "webnovel"
    assert {item["name"] for item in context.skills} == {
        "webnovel-init",
        "webnovel-plan",
        "webnovel-write",
        "webnovel-review",
        "webnovel-query",
        "webnovel-learn",
        "webnovel-dashboard",
        "webnovel-doctor",
    }
    assert all(item["path"].is_file() for item in context.skills)
    assert [name for name, _ in context.hooks] == ["pre_tool_call", "pre_llm_call"]
    assert [command["name"] for command in context.cli_commands] == ["webnovel"]


def test_runtime_schema_exposes_planned_task_actions_and_arguments() -> None:
    tool = registered_context().tools[0]
    schema = tool["schema"]
    parameters = schema["parameters"]

    assert schema["name"] == "webnovel_runtime"
    assert parameters["properties"]["action"]["enum"] == EXPECTED_ACTIONS
    assert set(parameters["properties"]) >= {
        "action",
        "project_root",
        "run_id",
        "chapter",
        "volume",
        "mode",
        "payload",
    }
    assert parameters["required"] == ["action", "project_root"]


def test_registered_runtime_handler_binds_official_context_llm_and_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = RecordingContext()
    context.llm = object()
    seen: dict[str, Any] = {}
    project = tmp_path / "session-project"
    metadata = project / ".webnovel"
    metadata.mkdir(parents=True)
    (metadata / "state.json").write_text(
        json.dumps(
            {
                "project_info": {"title": "会话小说"},
                "progress": {"current_chapter": 2, "total_words": 3000},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_handler(args: Any = None, *, llm: Any = None, **kwargs: Any) -> str:
        seen.update(args=args, llm=llm, kwargs=kwargs)
        return "{}"

    monkeypatch.setattr(webnovel_studio, "runtime_handler", fake_handler)
    webnovel_studio.register(context)
    args = {"action": "project_status", "project_root": str(project)}
    context.tools[0]["handler"](
        args,
        task_id="task-llm",
        session_id="session-tool-binding",
    )

    assert seen["llm"] is context.llm
    assert seen["args"] == args
    assert seen["kwargs"] == {
        "task_id": "task-llm",
        "session_id": "session-tool-binding",
    }
    hook = dict(context.hooks)["pre_llm_call"]
    injected = hook(
        session_id="session-tool-binding",
        user_message="继续",
        conversation_history=[],
    )
    assert injected is not None
    assert "会话小说" in injected["context"]


def test_runtime_handler_fails_closed_with_json_string(tmp_path: Path) -> None:
    handler = registered_context().tools[0]["handler"]
    project = tmp_path / "project"
    metadata = project / ".webnovel"
    metadata.mkdir(parents=True)
    (metadata / "state.json").write_text("{}\n", encoding="utf-8")

    raw_result = handler(
        {"action": "project_init", "project_root": str(project)},
        task_id="task-1",
    )
    result = json.loads(raw_result)

    assert isinstance(raw_result, str)
    assert result["success"] is False
    assert result["action"] == "project_init"
    assert result["project_root"] == str(project.resolve())
    assert result["status"] == "error"
    assert result["outputs"] == {}
    assert result["errors"][0]["code"] == "dependency_missing"


def test_task_two_hooks_are_no_ops() -> None:
    hooks = dict(registered_context().hooks)

    assert hooks["pre_tool_call"]("write_file", {"path": "draft.md"}, task_id="t1") is None
    assert hooks["pre_llm_call"](messages=[], session_id="s1") is None


def test_cli_parser_help_exits_zero_and_prints_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = registered_context().cli_commands[0]
    parser = argparse.ArgumentParser(prog="hermes webnovel")
    command["setup_fn"](parser)

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])

    assert exc_info.value.code == 0
    assert "usage: hermes webnovel" in capsys.readouterr().out.lower()


def test_cli_live_smoke_binds_host_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from webnovel_studio import live_smoke

    context = RecordingContext()
    context.llm = object()
    seen: dict[str, Any] = {}

    def fake_live(project_root: Path, *, llm: Any) -> dict[str, Any]:
        seen.update(project_root=project_root, llm=llm)
        return {"success": True, "project_root": str(project_root)}

    monkeypatch.setattr(live_smoke, "run_live_acceptance", fake_live)
    webnovel_studio.register(context)
    command = context.cli_commands[0]
    parser = argparse.ArgumentParser(prog="hermes webnovel")
    command["setup_fn"](parser)
    project = tmp_path / "live-project"
    command["handler_fn"](
        parser.parse_args(["smoke-live", "--project-root", str(project)])
    )

    assert seen == {"project_root": project, "llm": context.llm}
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_cli_plain_invocation_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    command = registered_context().cli_commands[0]
    parser = argparse.ArgumentParser(prog="hermes webnovel")
    command["setup_fn"](parser)

    with pytest.raises(SystemExit) as exc_info:
        command["handler_fn"](parser.parse_args([]))

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload == {"success": False, "error": "not_implemented"}


def test_real_hermes_registration_in_isolated_home(tmp_path: Path) -> None:
    hermes_executable = shutil.which("hermes")
    if hermes_executable is None:
        pytest.skip("Hermes CLI executable was not found on PATH")
    assert hermes_executable is not None

    temp_home = tmp_path / "hermes-home"
    plugin_dir = temp_home / "plugins" / "webnovel-writer"
    plugin_dir.parent.mkdir(parents=True)
    shutil.copytree(REPOSITORY_ROOT / "webnovel_studio", plugin_dir)
    (temp_home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(temp_home)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    def listed_plugin() -> dict[str, Any]:
        result = run(
            [hermes_executable, "plugins", "list", "--user", "--json"]
        )
        return next(
            item for item in json.loads(result.stdout)
            if item["name"] == "webnovel-writer"
        )

    assert listed_plugin()["status"] == "not enabled"

    run(
        [
            hermes_executable,
            "plugins",
            "enable",
            "webnovel-writer",
            "--no-allow-tool-override",
        ]
    )
    assert listed_plugin()["status"] == "enabled"

    help_result = run([hermes_executable, "webnovel", "--help"])
    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout.lower()

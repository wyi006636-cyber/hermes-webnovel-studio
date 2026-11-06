from __future__ import annotations

from pathlib import Path

import pytest

import webnovel_studio
from webnovel_studio.guards import pre_tool_call_guard


def assert_blocked(result: object) -> None:
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert set(result) == {"action", "message"}
    assert "webnovel_runtime" in result["message"]
    assert "accepted commit chain" in result["message"]


def assert_approval(result: object) -> None:
    assert isinstance(result, dict)
    assert result["action"] == "approve"
    assert result["rule_key"] == "webnovel:chapter-overwrite"
    assert set(result) == {"action", "message", "rule_key"}


@pytest.mark.parametrize("tool", ["write_file", "Write-File", "pkg/write", "tools.file_write"])
@pytest.mark.parametrize("key", ["path", "file_path", "target", "filename"])
def test_write_aliases_and_path_variants_block(tool: str, key: str) -> None:
    assert_blocked(pre_tool_call_guard(tool, {key: ".webnovel/state.json"}))


@pytest.mark.parametrize(
    "path",
    [
        ".story-system/events.jsonl",
        ".webnovel/state.json",
        ".webnovel/index.db",
        ".webnovel/index.db-wal",
        ".webnovel/memory.json",
        ".webnovel/memory_scratchpad.json",
        ".webnovel/project_memory.yaml",
        ".webnovel/summaries/ch0001.md",
        ".webnovel/summary/latest.md",
        ".webnovel/vector/store.bin",
        ".webnovel/vectors/store.bin",
        ".webnovel/projection/current.json",
        ".webnovel/projections/current.json",
    ],
)
def test_canonical_path_families_are_blocked(path: str) -> None:
    assert_blocked(pre_tool_call_guard("write", {"path": path}))


@pytest.mark.parametrize("tool", ["patch", "apply-patch", "pkg/edit_file", "edit"])
def test_patch_aliases_block_direct_path(tool: str) -> None:
    assert_blocked(pre_tool_call_guard(tool, {"path": ".story-system/state.json"}))


def test_chapter_write_requires_approval() -> None:
    assert_approval(pre_tool_call_guard("write_file", {"path": "小说/正文/第0001章.md"}))


def test_ordinary_write_is_allowed() -> None:
    assert pre_tool_call_guard("write_file", {"path": "设定集/人物.md"}) is None


def test_v4a_patch_collects_multiple_files_and_canonical_wins() -> None:
    patch = """*** Begin Patch
*** Update File: 小说/正文/第0001章.md
@@
-old
+new
*** Add File: .webnovel/memory-new.json
+{}
*** Delete File: notes/old.md
*** End Patch
"""
    assert_blocked(pre_tool_call_guard("apply_patch", {"patch": patch}))


def test_v4a_chapter_patch_requires_approval() -> None:
    patch = "*** Begin Patch\n*** Update File: 正文/第2章.md\n*** End Patch"
    assert_approval(pre_tool_call_guard("patch", {"patch": patch}))


@pytest.mark.parametrize(
    "command",
    [
        "rm .webnovel/state.json",
        "printf '{}' > .story-system/state.json",
        "printf x >> 正文/第3章.md",
        "echo x | tee .webnovel/memory.json",
        "sed -i '' 's/a/b/' .webnovel/state.json",
        "sed --in-place 's/a/b/' .webnovel/state.json",
        "find .story-system -type f -delete",
        "find .webnovel/memory -type f -exec rm {} \\;",
        "find /tmp -type f -fprint .story-system/events.jsonl",
        "find /tmp -type f -fprintf .webnovel/state.json '%p\\n'",
        "git diff --output .story-system/report.patch",
        "git diff --output=.story-system/report.patch",
        "python -c \"from pathlib import Path; Path('.webnovel/state.json').write_text('{}')\"",
        "python -c \"open('.webnovel/state.json', 'w').write('{}')\"",
        "python -c \"import os; os.remove('.story-system/events')\"",
    ],
)
def test_terminal_writes_to_canonical_paths_are_blocked(command: str) -> None:
    result = pre_tool_call_guard("terminal", {"command": command})
    if "正文" in command:
        assert_approval(result)
    else:
        assert_blocked(result)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("shell", {"cmd": "touch 正文/第4章.md"}),
        ("bash", {"script": "mv draft.md 正文/第4章.md"}),
        ("run-command", {"command": "cp draft.md 正文/第4章.md"}),
        ("tools/execute_command", {"command": "truncate -s 0 正文/第4章.md"}),
        ("execute_code", {"code": "open('正文/第4章.md', 'a').write('x')"}),
        ("code-exec", {"code": "Path('正文/第4章.md').write_bytes(b'x')"}),
    ],
)
def test_exec_aliases_require_chapter_approval(tool: str, args: dict[str, str]) -> None:
    assert_approval(pre_tool_call_guard(tool, args))


@pytest.mark.parametrize(
    "command",
    [
        "cat .webnovel/state.json",
        "cat .webnovel/state.json 2>&1",
        "head -n 2 .story-system/events.jsonl",
        "rg protagonist .webnovel/memory.json",
        "git diff -- .webnovel/state.json",
        "sed -n '1,4p' .webnovel/state.json",
        "python -c \"from pathlib import Path; print(Path('.webnovel/state.json').read_text())\"",
        "python -c \"print(open('.story-system/state.json', 'r').read())\"",
    ],
)
def test_explicit_read_only_commands_are_allowed(command: str) -> None:
    assert pre_tool_call_guard("terminal", {"command": command}) is None


def test_execute_code_read_only_is_allowed() -> None:
    code = "from pathlib import Path\nprint(Path('.webnovel/state.json').read_bytes())"
    assert pre_tool_call_guard("execute_code", {"code": code}) is None


def test_unknown_command_with_protected_marker_fails_closed() -> None:
    assert_blocked(pre_tool_call_guard("terminal", {"command": "mystery .webnovel/state.json"}))


@pytest.mark.parametrize(
    "command",
    [
        "cp .webnovel/state.json /tmp/state-backup.json",
        "cp -p 正文/第1章.md /tmp/chapter-backup.md",
        "cp .story-system/events.jsonl .webnovel/state.json /tmp/audit/",
    ],
)
def test_copying_protected_files_out_is_allowed(command: str) -> None:
    assert pre_tool_call_guard("terminal", {"command": command}) is None


def test_copying_into_protected_paths_is_still_guarded() -> None:
    assert_blocked(
        pre_tool_call_guard("terminal", {"command": "cp /tmp/state.json .webnovel/state.json"})
    )
    assert_approval(
        pre_tool_call_guard("terminal", {"command": "cp /tmp/chapter.md 正文/第2章.md"})
    )


def test_copy_out_combined_with_protected_delete_is_blocked() -> None:
    assert_blocked(
        pre_tool_call_guard(
            "terminal",
            {"command": "cp .webnovel/state.json /tmp/state.json && rm .webnovel/state.json"},
        )
    )


@pytest.mark.parametrize(
    "command",
    [
        "cp .webnovel/state.json > .webnovel/state.json /tmp/state-backup.json",
        "cp .webnovel/state.json /tmp/state <>.story-system/probe",
        "cp .webnovel/state.json /tmp/state >|.story-system/probe",
        "cp .webnovel/state.json /tmp/state &>.story-system/probe",
        "cp .webnovel/state.json /tmp/state >&.story-system/probe",
    ],
)
def test_copy_out_cannot_hide_protected_output_redirection(command: str) -> None:
    assert_blocked(
        pre_tool_call_guard(
            "terminal",
            {"command": command},
        )
    )


def test_copy_into_directory_checks_effective_child_symlink(tmp_path: Path) -> None:
    story = tmp_path / ".story-system"
    story.mkdir()
    canonical = story / "events.jsonl"
    canonical.write_text("{}\n", encoding="utf-8")
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "events.jsonl").symlink_to(canonical)

    assert_blocked(
        pre_tool_call_guard(
            "terminal",
            {"command": "cp .story-system/events.jsonl audit/", "cwd": str(tmp_path)},
        )
    )


def test_webnovel_directory_without_canonical_child_is_ordinary() -> None:
    assert pre_tool_call_guard("terminal", {"command": "mystery .webnovel/preferences.toml"}) is None


def test_metadata_root_delete_is_blocked_but_read_and_copy_out_are_allowed() -> None:
    assert_blocked(pre_tool_call_guard("terminal", {"command": "rm -rf .webnovel"}))
    assert pre_tool_call_guard("terminal", {"command": "ls .webnovel"}) is None
    assert pre_tool_call_guard("terminal", {"command": "cp -R .webnovel /tmp/novel-metadata"}) is None


@pytest.mark.parametrize(
    "path",
    [
        ".webnovel/runs/run-1/context/memory.json",
        ".webnovel/runs/run-1/data/projection.json",
        ".webnovel/tmp/memory-candidate.json",
        ".webnovel/backups/projection.json",
    ],
)
def test_run_staging_and_temporary_artifacts_are_not_canonical(path: str) -> None:
    assert pre_tool_call_guard("write_file", {"path": path}) is None


def test_destructive_find_following_symlinks_under_staging_is_blocked(tmp_path: Path) -> None:
    project = tmp_path / "project"
    runs = project / ".webnovel" / "runs"
    runs.mkdir(parents=True)
    commands = [
        ("find -L .webnovel/runs -type f -delete", project),
        ("find -L .webnovel/./runs -type f -delete", project),
        ("find .webnovel/runs -follow -type f -delete", project),
        ("find -L . -type f -delete", runs),
        ("find . -delete", project),
    ]
    for command, cwd in commands:
        assert_blocked(
            pre_tool_call_guard(
                "terminal",
                {"command": command, "cwd": str(cwd)},
            )
        )
    assert (
        pre_tool_call_guard(
            "terminal",
            {"command": "find .webnovel/runs -type f -delete", "cwd": str(project)},
        )
        is None
    )
    assert (
        pre_tool_call_guard(
            "terminal",
            {
                "command": "find -L .webnovel/runs -type f -fprint /tmp/run-files",
                "cwd": str(project),
            },
        )
        is None
    )


def test_destructive_ancestor_commands_are_blocked(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".webnovel").mkdir(parents=True)
    for command in (
        "rm -rf .",
        "rm -rf *",
        "rm -rf ../*",
        "chmod -R 700 .",
        "mv . ../moved",
        "find .. -delete",
    ):
        assert_blocked(
            pre_tool_call_guard(
                "terminal",
                {"command": command, "cwd": str(project)},
            )
        )
    assert_approval(
        pre_tool_call_guard(
            "terminal",
            {"command": "rm -rf 正文/*", "cwd": str(project)},
        )
    )
    assert (
        pre_tool_call_guard(
            "terminal",
            {"command": "rm -rf .webnovel/runs/*", "cwd": str(project)},
        )
        is None
    )


def test_windows_path_and_backslashes_are_classified() -> None:
    assert_blocked(
        pre_tool_call_guard("file-write", {"filename": r"C:\Books\Novel\.webnovel\state.json"})
    )


def test_traversal_and_quotes_cannot_hide_protected_path(tmp_path: Path) -> None:
    result = pre_tool_call_guard(
        "write_file",
        {"path": "'notes/../.webnovel/state.json'", "cwd": str(tmp_path)},
    )
    assert_blocked(result)


def test_symlink_parent_is_resolved_before_classification(tmp_path: Path) -> None:
    canonical = tmp_path / ".webnovel"
    canonical.mkdir()
    alias = tmp_path / "cache"
    alias.symlink_to(canonical, target_is_directory=True)

    assert_blocked(
        pre_tool_call_guard(
            "write_file",
            {"path": "cache/state.json", "workdir": str(tmp_path)},
        )
    )


def test_symlink_to_chapter_directory_requires_approval(tmp_path: Path) -> None:
    chapters = tmp_path / "正文"
    chapters.mkdir()
    alias = tmp_path / "drafts"
    alias.symlink_to(chapters, target_is_directory=True)
    assert_approval(
        pre_tool_call_guard("write_file", {"path": "drafts/1.md", "cwd": str(tmp_path)})
    )


def test_terminal_symlink_alias_cannot_hide_canonical_target(tmp_path: Path) -> None:
    canonical_dir = tmp_path / ".story-system"
    canonical_dir.mkdir()
    canonical = canonical_dir / "events.jsonl"
    canonical.write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "events-link"
    alias.symlink_to(canonical)

    assert_blocked(
        pre_tool_call_guard(
            "terminal",
            {"command": "rm events-link", "cwd": str(tmp_path)},
        )
    )
    assert pre_tool_call_guard(
        "terminal",
        {"command": "cat events-link", "cwd": str(tmp_path)},
    ) is None


def test_runtime_is_always_allowed() -> None:
    assert (
        pre_tool_call_guard(
            "tools/Webnovel-Runtime",
            {"path": ".webnovel/state.json", "command": "rm .story-system/events"},
        )
        is None
    )


def test_unrelated_tool_is_not_guarded() -> None:
    assert pre_tool_call_guard("read_file", {"path": ".webnovel/state.json"}) is None


def test_registered_hook_is_the_real_guard() -> None:
    class Context:
        def __init__(self) -> None:
            self.hooks: dict[str, object] = {}

        def register_tool(self, **kwargs: object) -> None:
            pass

        def register_hook(self, name: str, callback: object) -> None:
            self.hooks[name] = callback

        def register_skill(self, *args: object, **kwargs: object) -> None:
            pass

        def register_cli_command(self, **kwargs: object) -> None:
            pass

    context = Context()
    webnovel_studio.register(context)
    assert context.hooks["pre_tool_call"] is pre_tool_call_guard
    assert_blocked(context.hooks["pre_tool_call"]("write", {"path": ".webnovel/state.json"}))

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import webnovel_studio.run_store as run_store_module
from webnovel_studio.run_store import (
    MAIN_STAGES,
    AtomicRunWriteError,
    CorruptRunError,
    InvalidRunIdError,
    InvalidTransitionError,
    RunStore,
    RunStoreError,
)


def make_project(path: Path) -> Path:
    path.mkdir(parents=True)
    metadata = path / ".webnovel"
    metadata.mkdir()
    (metadata / "state.json").write_text("{}\n", encoding="utf-8")
    return path.resolve()


def create_run(store: RunStore, run_id: str = "run-001") -> dict[str, Any]:
    return store.create(
        run_id,
        chapter=12,
        volume=2,
        mode="章节写作",
        request={"prompt": "写一章雨夜重逢", "语言": "中文"},
    )


def run_path(project: Path, run_id: str = "run-001") -> Path:
    return project / ".webnovel" / "runs" / run_id / "run.json"


def rewrite_record(path: Path, mutate: Any) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable on this platform: {exc}")


def test_create_persists_required_utf8_fields_and_loads_a_copy(tmp_path: Path) -> None:
    project = make_project(tmp_path / "中文项目")
    store = RunStore(project)

    created = create_run(store)
    run_path = project / ".webnovel" / "runs" / "run-001" / "run.json"
    raw = run_path.read_text(encoding="utf-8")
    loaded = store.load("run-001")

    assert "中文项目" in raw
    assert "雨夜重逢" in raw
    assert created == loaded
    assert loaded["schema_version"] == 1
    assert loaded["run_id"] == "run-001"
    assert loaded["status"] == "NEW"
    assert loaded["project_root"] == str(project)
    assert loaded["chapter"] == 12
    assert loaded["volume"] == 2
    assert loaded["mode"] == "章节写作"
    assert loaded["request"]["语言"] == "中文"
    assert loaded["revision"] == 0
    assert loaded["resume_count"] == 0
    assert loaded["error"] is None
    assert loaded["history"] == [
        {
            "from": None,
            "to": "NEW",
            "time": loaded["created_at"],
            "reason": "created",
        }
    ]
    datetime.fromisoformat(loaded["created_at"].replace("Z", "+00:00"))
    assert loaded["updated_at"] == loaded["created_at"]


def test_legal_full_main_chain_updates_revision_timestamps_and_history(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    record = create_run(store)

    for expected_revision, target in enumerate(MAIN_STAGES[1:], start=1):
        previous = record
        record = store.transition("run-001", target, reason=f"完成 {target}")
        assert record["status"] == target
        assert record["revision"] == expected_revision
        assert record["updated_at"] >= previous["updated_at"]
        assert record["history"][-1]["from"] == previous["status"]
        assert record["history"][-1]["to"] == target
        assert record["history"][-1]["reason"] == f"完成 {target}"

    assert record["status"] == "COMPLETED"
    assert len(record["history"]) == len(MAIN_STAGES)


def test_illegal_skip_unknown_stage_and_terminal_change_are_rejected(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)

    with pytest.raises(InvalidTransitionError, match="NEW.*BRIEF_READY"):
        store.transition("run-001", "BRIEF_READY")
    with pytest.raises(InvalidTransitionError, match="unknown stage"):
        store.transition("run-001", "MAGIC")

    for stage in MAIN_STAGES[1:]:
        store.transition("run-001", stage)
    with pytest.raises(InvalidTransitionError, match="terminal"):
        store.transition("run-001", "COMPLETED")
    with pytest.raises(InvalidTransitionError, match="terminal"):
        store.abort("run-001", reason="too late")


def test_fail_resume_and_only_corresponding_target_stage_is_allowed(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)
    store.transition("run-001", "PREPARED")

    failed = store.fail(
        "run-001", "BRIEF_READY", "提纲服务暂时不可用", reason="brief failed"
    )
    assert failed["status"] == "FAILED_BRIEF_READY"
    assert failed["error"]["target_stage"] == "BRIEF_READY"
    assert failed["error"]["retry_count"] == 1

    with pytest.raises(InvalidTransitionError, match="BRIEF_READY"):
        store.transition("run-001", "DRAFT_READY")
    with pytest.raises(InvalidTransitionError, match="BRIEF_READY"):
        store.fail("run-001", "DRAFT_READY", "wrong retry")

    resumed = store.resume("run-001", reason="operator retry")
    assert resumed["status"] == "FAILED_BRIEF_READY"
    assert resumed["resume_count"] == 1
    assert resumed["resume"]["target_stage"] == "BRIEF_READY"
    assert resumed["history"][-1]["from"] == "FAILED_BRIEF_READY"
    assert resumed["history"][-1]["to"] == "FAILED_BRIEF_READY"

    ready = store.transition("run-001", "BRIEF_READY", reason="retry succeeded")
    assert ready["status"] == "BRIEF_READY"
    assert ready["error"] is None


def test_second_failure_increments_per_target_retry_count(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)

    first = store.fail("run-001", "PREPARED", {"message": "first", "code": 503})
    second = store.fail("run-001", "PREPARED", "second")

    assert first["error"]["retry_count"] == 1
    assert second["error"]["retry_count"] == 2
    assert second["retry_counts"] == {"PREPARED": 2}
    assert second["revision"] == 2


def test_failure_history_keeps_independent_complete_snapshots_after_success(
    tmp_path: Path,
) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)
    first_error = {"message": "first", "code": 503, "details": {"attempt": [1]}}

    first = store.fail("run-001", "PREPARED", first_error)
    first_error["details"]["attempt"].append(999)
    first["error"]["details"]["attempt"].append(888)
    store.fail("run-001", "PREPARED", {"message": "second", "code": 429})
    succeeded = store.transition("run-001", "PREPARED", reason="retry succeeded")

    failure_entries = [entry for entry in succeeded["history"] if "error" in entry]
    assert succeeded["error"] is None
    assert len(failure_entries) == 2
    assert failure_entries[0]["error"]["message"] == "first"
    assert failure_entries[0]["error"]["code"] == 503
    assert failure_entries[0]["error"]["details"] == {"attempt": [1]}
    assert failure_entries[0]["error"]["retry_count"] == 1
    assert failure_entries[1]["error"]["message"] == "second"
    assert failure_entries[1]["error"]["code"] == 429
    assert failure_entries[1]["error"]["retry_count"] == 2
    json.dumps(failure_entries, ensure_ascii=False, allow_nan=False)


def test_abort_before_commit_is_terminal_without_accepted_commit(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)
    store.transition("run-001", "PREPARED")

    aborted = store.abort("run-001", reason="用户取消")
    assert aborted["status"] == "ABORTED"
    assert aborted["accepted_commit"] is False
    assert aborted["abort"]["accepted_commit"] is False
    with pytest.raises(InvalidTransitionError, match="terminal"):
        store.resume("run-001")
    with pytest.raises(InvalidTransitionError, match="terminal"):
        store.fail("run-001", "BRIEF_READY", "no")


def test_abort_at_or_after_commit_marks_accepted_commit_without_deleting_artifacts(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    create_run(store)
    for stage in MAIN_STAGES[1 : MAIN_STAGES.index("COMMITTED") + 1]:
        store.transition("run-001", stage)
    artifact = project / ".webnovel" / "runs" / "run-001" / "accepted.txt"
    artifact.write_text("keep me", encoding="utf-8")

    aborted = store.abort("run-001", reason="projection failed permanently")

    assert aborted["status"] == "ABORTED"
    assert aborted["accepted_commit"] is True
    assert aborted["abort"]["accepted_commit"] is True
    assert artifact.read_text(encoding="utf-8") == "keep me"


def test_abort_after_failure_beyond_commit_also_preserves_accepted_fact(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)
    for stage in MAIN_STAGES[1 : MAIN_STAGES.index("COMMITTED") + 1]:
        store.transition("run-001", stage)
    store.fail("run-001", "PROJECTED", "projection failed")

    assert store.abort("run-001")["accepted_commit"] is True


def test_atomic_replace_failure_keeps_previous_json_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    create_run(store)
    run_dir = project / ".webnovel" / "runs" / "run-001"
    run_path = run_dir / "run.json"
    previous = run_path.read_bytes()

    def fail_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr("webnovel_studio.run_store.os.replace", fail_replace)
    with pytest.raises(RunStoreError, match="atomic.*run.json"):
        store.transition("run-001", "PREPARED")

    assert run_path.read_bytes() == previous
    assert [path for path in run_dir.iterdir() if path.name != "run.json"] == []


def test_corrupt_json_and_invalid_record_are_reported_clearly(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    create_run(store)
    run_path = project / ".webnovel" / "runs" / "run-001" / "run.json"

    run_path.write_text("{ definitely not json", encoding="utf-8")
    with pytest.raises(CorruptRunError, match="invalid JSON.*run.json"):
        store.load("run-001")

    run_path.write_text(json.dumps({"run_id": "other", "status": "NEW"}), encoding="utf-8")
    with pytest.raises(CorruptRunError, match="does not match"):
        store.load("run-001")


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        ".",
        "..",
        "../escape",
        "nested/run",
        r"nested\run",
        "/absolute",
        "CON",
        "con.txt",
        "PRN.log",
        "AUX",
        "NUL.data",
        "COM1",
        "com9.txt",
        "LPT1",
        "lpt9.out",
        "trailing.",
        "trailing ",
    ],
)
def test_run_id_path_traversal_is_rejected(tmp_path: Path, run_id: str) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)

    with pytest.raises(InvalidRunIdError, match="run_id"):
        store.create(run_id, chapter=1, volume=1, mode="write", request="request")
    assert not (project / "escape").exists()


def test_duplicate_run_and_missing_run_are_clear_errors(tmp_path: Path) -> None:
    store = RunStore(make_project(tmp_path / "project"))
    create_run(store)

    with pytest.raises(RunStoreError, match="already exists"):
        create_run(store)
    with pytest.raises(RunStoreError, match="not found"):
        store.load("missing")


def test_project_marker_is_validated_again_before_each_write(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    create_run(store)
    (project / ".webnovel" / "state.json").unlink()

    with pytest.raises(RunStoreError, match=r"missing .*\.webnovel/state\.json"):
        store.transition("run-001", "PREPARED")


def test_runs_directory_symlink_is_rejected_before_create(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    symlink_or_skip(outside, project / ".webnovel" / "runs", directory=True)

    with pytest.raises(RunStoreError, match=r"runs.*symlink"):
        create_run(store)
    assert list(outside.iterdir()) == []


def test_run_directory_symlink_is_rejected_before_create(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    runs = project / ".webnovel" / "runs"
    runs.mkdir()
    outside = tmp_path / "outside-run"
    outside.mkdir()
    symlink_or_skip(outside, runs / "run-001", directory=True)

    with pytest.raises(RunStoreError, match=r"run directory.*symlink"):
        create_run(store)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("operation", ["load", "mutate"])
def test_run_json_symlink_is_rejected_before_access(
    tmp_path: Path, operation: str
) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    create_run(store)
    ledger = run_path(project)
    outside = tmp_path / "outside-run.json"
    outside.write_text('{"sentinel": true}\n', encoding="utf-8")
    ledger.unlink()
    symlink_or_skip(outside, ledger)

    with pytest.raises(RunStoreError, match=r"run\.json.*symlink"):
        if operation == "load":
            store.load("run-001")
        else:
            store.transition("run-001", "PREPARED")
    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}\n'


@pytest.mark.parametrize("component", ["metadata", "state"])
def test_project_component_symlink_is_rejected_again_before_run_operation(
    tmp_path: Path, component: str
) -> None:
    project = make_project(tmp_path / "project")
    store = RunStore(project)
    metadata = project / ".webnovel"

    if component == "metadata":
        outside = tmp_path / "outside-metadata"
        metadata.rename(outside)
        symlink_or_skip(outside, metadata, directory=True)
        pattern = r"\.webnovel.*symlink"
    else:
        state = metadata / "state.json"
        outside = tmp_path / "outside-state.json"
        state.rename(outside)
        symlink_or_skip(outside, state)
        pattern = r"state\.json.*symlink"

    with pytest.raises(RunStoreError, match=pattern):
        create_run(store)


def _tamper_record(record: dict[str, Any], case: str) -> None:
    if case == "accepted_commit_string":
        record["accepted_commit"] = "false"
    elif case == "retry_count_bool":
        record["retry_counts"]["PREPARED"] = True
    elif case == "retry_count_negative":
        record["retry_counts"]["PREPARED"] = -1
    elif case == "resume_count_bool":
        record["resume_count"] = True
    elif case == "bad_created_timestamp":
        record["created_at"] = "yesterday"
    elif case == "non_monotonic_history":
        record["history"][-1]["time"] = (
            datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        record["updated_at"] = record["history"][-1]["time"]
    elif case == "bad_history_first":
        record["history"][0]["from"] = "NEW"
    elif case == "bad_history_last":
        record["history"][-1]["to"] = "BRIEF_READY"
    elif case == "history_entry_missing_reason":
        record["history"][-1].pop("reason")
    elif case == "failed_error_target":
        record["error"]["target_stage"] = "BRIEF_READY"
    elif case == "failed_error_count":
        record["error"]["retry_count"] = 99
    elif case == "missing_required":
        record.pop("retry_counts")
    elif case == "aborted_missing_abort":
        record.pop("abort")
    elif case == "non_json_nan":
        record["request"] = float("nan")
    elif case == "illegal_history_transition":
        record["history"][-1]["to"] = "DRAFT_READY"
        record["status"] = "DRAFT_READY"
    elif case == "aborted_false_commit_tampered_true":
        record["accepted_commit"] = True
        record["abort"]["accepted_commit"] = True
    else:  # pragma: no cover - protects the parameter table itself.
        raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "state"),
    [
        ("accepted_commit_string", "new"),
        ("retry_count_bool", "failed"),
        ("retry_count_negative", "failed"),
        ("resume_count_bool", "new"),
        ("bad_created_timestamp", "new"),
        ("non_monotonic_history", "prepared"),
        ("bad_history_first", "new"),
        ("bad_history_last", "prepared"),
        ("history_entry_missing_reason", "new"),
        ("failed_error_target", "failed"),
        ("failed_error_count", "failed"),
        ("missing_required", "new"),
        ("aborted_missing_abort", "aborted"),
        ("non_json_nan", "new"),
        ("illegal_history_transition", "prepared"),
        ("aborted_false_commit_tampered_true", "aborted"),
    ],
)
def test_complete_schema_tampering_always_raises_corrupt_run(
    tmp_path: Path, case: str, state: str
) -> None:
    project = make_project(tmp_path / case)
    store = RunStore(project)
    create_run(store)
    if state == "prepared":
        store.transition("run-001", "PREPARED")
    elif state == "failed":
        store.fail("run-001", "PREPARED", "failure")
    elif state == "aborted":
        store.abort("run-001")

    rewrite_record(run_path(project), lambda record: _tamper_record(record, case))

    with pytest.raises(CorruptRunError):
        store.load("run-001")


def test_duplicate_ledger_key_is_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path / "duplicate-ledger-key")
    store = RunStore(project)
    create_run(store)
    path = run_path(project)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"run_id": "run-001",', '"run_id": "run-001",\n  "run_id": "forged",', 1), encoding="utf-8")

    with pytest.raises(CorruptRunError, match="duplicate JSON key"):
        store.load("run-001")


def test_directory_fsync_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path / "fsync-failure")
    store = RunStore(project)
    real_fsync = run_store_module.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(run_store_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(AtomicRunWriteError, match="directory fsync failure"):
        create_run(store)

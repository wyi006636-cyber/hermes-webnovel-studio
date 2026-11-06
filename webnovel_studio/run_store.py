"""Durable, resumable chapter-run ledger.

``RunStore`` owns only ``.webnovel/runs/<run_id>/run.json`` records.  It does
not call an LLM, project canonical story state, or acquire another lock; callers
must hold :class:`webnovel_studio.project_lock.ProjectLock` around workflows that
need single-writer behavior.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn


SCHEMA_VERSION = 1
MAIN_STAGES: tuple[str, ...] = (
    "NEW",
    "PREPARED",
    "BRIEF_READY",
    "DRAFT_READY",
    "REVIEW_READY",
    "REVISED_READY",
    "ARTIFACTS_READY",
    "COMMITTED",
    "PROJECTED",
    "BACKED_UP",
    "COMPLETED",
)
TERMINAL_STAGES = frozenset({"ABORTED", "COMPLETED"})
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "status",
        "project_root",
        "chapter",
        "volume",
        "mode",
        "request",
        "created_at",
        "updated_at",
        "revision",
        "resume_count",
        "retry_counts",
        "error",
        "resume",
        "accepted_commit",
        "abort",
        "history",
    }
)


class RunStoreError(RuntimeError):
    """Base error for durable run-ledger operations."""


class InvalidRunIdError(RunStoreError, ValueError):
    """Raised when a run identifier could escape or confuse its directory."""


class InvalidTransitionError(RunStoreError, ValueError):
    """Raised for an unknown, skipped, or terminal state transition."""


class CorruptRunError(RunStoreError):
    """Raised when an existing run ledger cannot be trusted."""


class AtomicRunWriteError(RunStoreError):
    """Raised when an atomic run.json replacement fails."""


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _timestamp_after(previous: str) -> str:
    now = datetime.now(timezone.utc)
    try:
        prior = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _timestamp()
    if now <= prior:
        now = prior + timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_run_id(run_id: str) -> str:
    invalid = (
        not isinstance(run_id, str)
        or not _RUN_ID_PATTERN.fullmatch(run_id)
        or run_id.endswith((".", " "))
    )
    if not invalid:
        basename = run_id.split(".", 1)[0].upper()
        invalid = basename in _WINDOWS_RESERVED_BASENAMES
    if invalid:
        raise InvalidRunIdError(
            "invalid run_id; expected a portable 1-128 character ASCII filename "
            "without path separators, trailing dots/spaces, or Windows device names: "
            f"{run_id!r}"
        )
    return run_id


def _failed_target(status: str) -> str | None:
    prefix = "FAILED_"
    if status.startswith(prefix):
        target = status[len(prefix) :]
        if target in MAIN_STAGES and target != "NEW":
            return target
    return None


def _validate_stage(stage: str) -> str:
    if stage not in MAIN_STAGES:
        raise InvalidTransitionError(f"unknown stage: {stage!r}")
    return stage


def _next_stage(stage: str) -> str | None:
    index = MAIN_STAGES.index(stage)
    if index + 1 == len(MAIN_STAGES):
        return None
    return MAIN_STAGES[index + 1]


def _ensure_serializable(value: Any, *, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RunStoreError(f"{label} is not JSON serializable: {exc}") from exc


def _validate_project(project_root: str | os.PathLike[str]) -> Path:
    root = Path(project_root).expanduser().resolve(strict=False)
    if not root.exists():
        raise RunStoreError(f"project does not exist: {root}")
    if not root.is_dir():
        raise RunStoreError(f"project is not a directory: {root}")

    metadata = root / ".webnovel"
    marker = metadata / "state.json"
    if metadata.is_symlink():
        raise RunStoreError(f"project .webnovel must not be a symlink: {metadata}")
    if marker.is_symlink():
        raise RunStoreError(f"project state.json must not be a symlink: {marker}")
    if not marker.is_file():
        raise RunStoreError(
            f"project is missing required .webnovel/state.json: {root}"
        )
    _require_contained(root, metadata, label=".webnovel", strict=True)
    _require_contained(root, marker, label="state.json", strict=True)
    return root


def _require_contained(
    project_root: Path, path: Path, *, label: str, strict: bool = False
) -> None:
    try:
        path.resolve(strict=strict).relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise RunStoreError(
            f"{label} resolves outside project root: {path}"
        ) from exc


def _reject_symlink(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise RunStoreError(f"{label} must not be a symlink: {path}")


def _parse_utc_timestamp(value: Any, *, label: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise CorruptRunError(f"run ledger {label} is not a UTC ISO timestamp: {path}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise CorruptRunError(
            f"run ledger {label} is not a UTC ISO timestamp: {path}"
        ) from exc
    if parsed.tzinfo is None or offset != timedelta(0):
        raise CorruptRunError(f"run ledger {label} is not a UTC ISO timestamp: {path}")
    return parsed


def _is_exact_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace ``path`` atomically, preserving the old file on every failure."""

    _ensure_serializable(payload, label="run record")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory_best_effort(path.parent)
    except Exception as exc:
        raise AtomicRunWriteError(f"atomic write failed for {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory_best_effort(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RunStore:
    """Create and advance durable run records for a single novel project."""

    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        self.project_root = _validate_project(project_root)
        self.runs_root = self.project_root / ".webnovel" / "runs"

    def run_path(self, run_id: str) -> Path:
        """Return the safe canonical path for ``run_id`` without creating it."""

        safe_id = _validate_run_id(run_id)
        return self._ledger_path(safe_id)

    def _ledger_path(self, safe_id: str) -> Path:
        """Revalidate every storage component and return the lexical ledger path."""

        _validate_project(self.project_root)
        metadata = self.project_root / ".webnovel"
        runs_root = metadata / "runs"
        run_directory = runs_root / safe_id
        ledger = run_directory / "run.json"

        for path, label in (
            (runs_root, ".webnovel/runs"),
            (run_directory, f"run directory {safe_id!r}"),
            (ledger, "run.json"),
        ):
            _reject_symlink(path, label=label)
            _require_contained(self.project_root, path, label=label)

        if runs_root.exists() and not runs_root.is_dir():
            raise RunStoreError(f".webnovel/runs is not a directory: {runs_root}")
        if run_directory.exists() and not run_directory.is_dir():
            raise RunStoreError(f"run directory is not a directory: {run_directory}")
        if ledger.exists() and not ledger.is_file():
            raise RunStoreError(f"run.json is not a regular file: {ledger}")
        return ledger

    def create(
        self,
        run_id: str,
        *,
        chapter: int | None = None,
        volume: int | None = None,
        mode: str | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        """Create a NEW run and persist its initial history entry."""

        _validate_project(self.project_root)
        safe_id = _validate_run_id(run_id)
        self._validate_positive_number(chapter, label="chapter")
        self._validate_positive_number(volume, label="volume")
        if mode is not None and not isinstance(mode, str):
            raise RunStoreError("mode must be a string or null")
        _ensure_serializable(request, label="request")

        path = self.run_path(safe_id)
        run_directory = path.parent
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RunStoreError(f"run already exists: {safe_id} ({path})") from exc
        created_directory = True
        now = _timestamp()
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": safe_id,
            "status": "NEW",
            "project_root": str(self.project_root),
            "chapter": chapter,
            "volume": volume,
            "mode": mode,
            "request": copy.deepcopy(request),
            "created_at": now,
            "updated_at": now,
            "revision": 0,
            "resume_count": 0,
            "retry_counts": {},
            "error": None,
            "resume": None,
            "accepted_commit": False,
            "abort": None,
            "history": [
                {"from": None, "to": "NEW", "time": now, "reason": "created"}
            ],
        }
        try:
            self._validate_record(record, expected_run_id=safe_id, path=path)
            _atomic_write_json(path, record)
        except Exception:
            if created_directory:
                try:
                    run_directory.rmdir()
                except OSError:
                    pass
            raise
        return copy.deepcopy(record)

    def load(self, run_id: str) -> dict[str, Any]:
        """Load and minimally validate an existing UTF-8 JSON ledger."""

        _validate_project(self.project_root)
        safe_id = _validate_run_id(run_id)
        path = self.run_path(safe_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(
                    handle,
                    parse_constant=_reject_constant,
                    object_pairs_hook=_reject_duplicate_keys,
                )
        except FileNotFoundError as exc:
            raise RunStoreError(f"run not found: {safe_id} ({path})") from exc
        except (OSError, UnicodeError) as exc:
            raise CorruptRunError(f"cannot read run ledger {path}: {exc}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise CorruptRunError(f"invalid JSON in run ledger {path}: {exc}") from exc

        try:
            self._validate_record(record, expected_run_id=safe_id, path=path)
        except CorruptRunError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            raise CorruptRunError(f"run ledger schema is invalid: {path}: {exc}") from exc
        return record

    def _validate_record(
        self, record: Any, *, expected_run_id: str, path: Path
    ) -> None:
        """Validate the complete v1 ledger schema and all cross-field invariants."""

        def corrupt(message: str) -> NoReturn:
            raise CorruptRunError(f"run ledger {message}: {path}")

        if not isinstance(record, dict):
            corrupt("is not a JSON object")
        try:
            json.dumps(record, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            corrupt(f"contains a non-JSON value: {exc}")
        if record.get("run_id") != expected_run_id:
            corrupt(f"run_id does not match directory {expected_run_id!r}")
        missing = sorted(_REQUIRED_FIELDS.difference(record))
        if missing:
            corrupt(f"is missing required fields {missing}")

        if not _is_exact_int(record["schema_version"]) or record["schema_version"] != SCHEMA_VERSION:
            corrupt(f"has unsupported schema_version {record['schema_version']!r}")
        if record["project_root"] != str(self.project_root):
            corrupt("project_root does not match store")

        status = record["status"]
        if not isinstance(status, str) or (
            status not in MAIN_STAGES
            and status not in TERMINAL_STAGES
            and _failed_target(status) is None
        ):
            corrupt(f"has unknown status {status!r}")

        for label in ("chapter", "volume"):
            value = record[label]
            if value is not None and (not _is_exact_int(value) or value < 1):
                corrupt(f"{label} is invalid")
        if record["mode"] is not None and not isinstance(record["mode"], str):
            corrupt("mode is invalid")
        if not _is_exact_int(record["revision"]) or record["revision"] < 0:
            corrupt("revision is invalid")
        if not _is_exact_int(record["resume_count"]) or record["resume_count"] < 0:
            corrupt("resume_count is invalid")
        if not isinstance(record["accepted_commit"], bool):
            corrupt("accepted_commit must be a boolean")

        created_at = _parse_utc_timestamp(record["created_at"], label="created_at", path=path)
        updated_at = _parse_utc_timestamp(record["updated_at"], label="updated_at", path=path)
        if updated_at < created_at:
            corrupt("updated_at precedes created_at")

        retry_counts = record["retry_counts"]
        if not isinstance(retry_counts, dict):
            corrupt("retry_counts is not an object")
        for target, count in retry_counts.items():
            if target not in MAIN_STAGES or target == "NEW":
                corrupt(f"retry_counts has unknown target {target!r}")
            if not _is_exact_int(count) or count < 1:
                corrupt(f"retry_counts[{target!r}] is invalid")

        history = record["history"]
        if not isinstance(history, list) or not history:
            corrupt("history must be a non-empty list")
        if len(history) != record["revision"] + 1:
            corrupt("history length does not match revision")

        history_times: list[datetime] = []
        failure_counts: dict[str, int] = {}
        latest_failure_error: dict[str, Any] | None = None
        resume_entries = 0
        previous_to: str | None = None
        for index, entry in enumerate(history):
            if not isinstance(entry, dict):
                corrupt(f"history entry {index} is not an object")
            if not {"from", "to", "time", "reason"}.issubset(entry):
                corrupt(f"history entry {index} is missing required fields")
            from_stage = entry["from"]
            to_stage = entry["to"]
            if from_stage is not None and not self._is_record_status(from_stage):
                corrupt(f"history entry {index} has invalid from stage")
            if not self._is_record_status(to_stage):
                corrupt(f"history entry {index} has invalid to stage")
            if not isinstance(entry["reason"], str):
                corrupt(f"history entry {index} reason is invalid")
            entry_time = _parse_utc_timestamp(
                entry["time"], label=f"history[{index}].time", path=path
            )
            if history_times and entry_time < history_times[-1]:
                corrupt("history timestamps are not monotonic")
            if entry_time < created_at or entry_time > updated_at:
                corrupt(f"history entry {index} timestamp is out of bounds")
            history_times.append(entry_time)

            has_error_snapshot = "error" in entry
            if index == 0:
                if from_stage is not None or to_stage != "NEW":
                    corrupt("history first entry must create NEW from null")
                if entry_time != created_at:
                    corrupt("history first timestamp does not match created_at")
            else:
                if from_stage != previous_to:
                    corrupt(f"history entry {index} does not continue prior status")
                if not isinstance(from_stage, str):
                    corrupt(f"history entry {index} has invalid from stage")
                if not self._is_valid_history_transition(
                    from_stage, to_stage, has_error_snapshot=has_error_snapshot
                ):
                    corrupt(f"history entry {index} is an illegal transition")
            previous_to = to_stage

            error_snapshot = entry.get("error") if has_error_snapshot else None
            failed_target = _failed_target(to_stage)
            if has_error_snapshot:
                if failed_target is None or not isinstance(error_snapshot, dict):
                    corrupt(f"history entry {index} error snapshot is invalid")
                self._validate_error_payload(
                    error_snapshot,
                    target=failed_target,
                    at=entry["time"],
                    path=path,
                    label=f"history[{index}].error",
                )
                expected_count = failure_counts.get(failed_target, 0) + 1
                if error_snapshot["retry_count"] != expected_count:
                    corrupt(f"history entry {index} retry_count is not sequential")
                failure_counts[failed_target] = expected_count
                latest_failure_error = error_snapshot
            elif from_stage == to_stage and _failed_target(to_stage) is not None:
                resume_entries += 1

        if history[-1]["to"] != status:
            corrupt("history last entry does not match status")
        if history_times[-1] != updated_at:
            corrupt("history last timestamp does not match updated_at")
        if retry_counts != failure_counts:
            corrupt("retry_counts does not match failure history")
        if record["resume_count"] != resume_entries:
            corrupt("resume_count does not match resume history")

        error = record["error"]
        failed_target = _failed_target(status)
        if failed_target is not None:
            if not isinstance(error, dict):
                corrupt("FAILED status requires an error object")
            self._validate_error_payload(
                error,
                target=failed_target,
                at=None,
                path=path,
                label="error",
            )
            if error["retry_count"] != retry_counts.get(failed_target):
                corrupt("FAILED error retry_count does not match retry_counts")
            if latest_failure_error is None or error != latest_failure_error:
                corrupt("FAILED error does not match latest failure history snapshot")
        elif status != "ABORTED" and error is not None:
            corrupt("non-failed status has a non-null error")

        self._validate_resume(record, history=history, path=path)
        self._validate_abort(record, path=path)
        self._validate_accepted_commit(record, status=status, path=path)

    @staticmethod
    def _is_record_status(value: Any) -> bool:
        return isinstance(value, str) and (
            value in MAIN_STAGES
            or value in TERMINAL_STAGES
            or _failed_target(value) is not None
        )

    @staticmethod
    def _is_valid_history_transition(
        from_stage: str,
        to_stage: str,
        *,
        has_error_snapshot: bool,
    ) -> bool:
        """Validate one immutable ledger-history edge."""

        if from_stage in TERMINAL_STAGES:
            return False
        if to_stage == "ABORTED":
            return not has_error_snapshot

        failed_target = _failed_target(from_stage)
        if failed_target is not None:
            if to_stage == from_stage:
                # A repeated failure carries an error; a resume marker does not.
                return True
            return to_stage == failed_target and not has_error_snapshot

        if from_stage not in MAIN_STAGES:
            return False
        expected = _next_stage(from_stage)
        if expected is None:
            return False
        if has_error_snapshot:
            return to_stage == f"FAILED_{expected}"
        return to_stage == expected

    @staticmethod
    def _validate_error_payload(
        payload: dict[str, Any],
        *,
        target: str,
        at: str | None,
        path: Path,
        label: str,
    ) -> None:
        if payload.get("target_stage") != target:
            raise CorruptRunError(
                f"run ledger {label} target_stage does not match {target}: {path}"
            )
        count = payload.get("retry_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise CorruptRunError(f"run ledger {label} retry_count is invalid: {path}")
        parsed_at = _parse_utc_timestamp(payload.get("at"), label=f"{label}.at", path=path)
        if at is not None and payload["at"] != at:
            raise CorruptRunError(
                f"run ledger {label} timestamp does not match history entry: {path}"
            )
        del parsed_at

    def _validate_resume(
        self, record: dict[str, Any], *, history: list[dict[str, Any]], path: Path
    ) -> None:
        resume = record["resume"]
        count = record["resume_count"]
        if count == 0:
            if resume is not None:
                raise CorruptRunError(
                    f"run ledger resume must be null when resume_count is zero: {path}"
                )
            return
        if not isinstance(resume, dict):
            raise CorruptRunError(f"run ledger resume is invalid: {path}")
        required = {"target_stage", "at", "count", "reason"}
        if not required.issubset(resume):
            raise CorruptRunError(f"run ledger resume is missing required fields: {path}")
        target = resume["target_stage"]
        if target not in MAIN_STAGES or target == "NEW":
            raise CorruptRunError(f"run ledger resume target_stage is invalid: {path}")
        if not _is_exact_int(resume["count"]) or resume["count"] != count:
            raise CorruptRunError(f"run ledger resume count is invalid: {path}")
        if not isinstance(resume["reason"], str):
            raise CorruptRunError(f"run ledger resume reason is invalid: {path}")
        _parse_utc_timestamp(resume["at"], label="resume.at", path=path)
        failed_status = f"FAILED_{target}"
        if not any(
            entry["from"] == failed_status
            and entry["to"] == failed_status
            and entry["time"] == resume["at"]
            and entry["reason"] == resume["reason"]
            and "error" not in entry
            for entry in history
        ):
            raise CorruptRunError(f"run ledger resume does not match history: {path}")

    @staticmethod
    def _validate_abort(record: dict[str, Any], *, path: Path) -> None:
        abort = record["abort"]
        if record["status"] != "ABORTED":
            if abort is not None:
                raise CorruptRunError(
                    f"run ledger abort must be null before ABORTED: {path}"
                )
            return
        if not isinstance(abort, dict):
            raise CorruptRunError(f"run ledger ABORTED status requires abort: {path}")
        required = {"at", "reason", "accepted_commit", "artifacts_preserved"}
        if not required.issubset(abort):
            raise CorruptRunError(f"run ledger abort is missing required fields: {path}")
        _parse_utc_timestamp(abort["at"], label="abort.at", path=path)
        if (
            abort["at"] != record["updated_at"]
            or not isinstance(abort["reason"], str)
            or abort["accepted_commit"] is not record["accepted_commit"]
            or abort["artifacts_preserved"] is not True
        ):
            raise CorruptRunError(f"run ledger abort invariants are invalid: {path}")

    @staticmethod
    def _validate_accepted_commit(
        record: dict[str, Any], *, status: str, path: Path
    ) -> None:
        if status == "ABORTED":
            abort_from = record["history"][-1]["from"]
            expected = bool(
                isinstance(abort_from, str)
                and RunStore._is_after_commit(abort_from)
            )
        elif status == "COMPLETED":
            expected = True
        elif status in MAIN_STAGES:
            expected = MAIN_STAGES.index(status) >= MAIN_STAGES.index("COMMITTED")
        else:
            target = _failed_target(status)
            expected = bool(
                target is not None
                and MAIN_STAGES.index(target) > MAIN_STAGES.index("COMMITTED")
            )
        if record["accepted_commit"] is not expected:
            raise CorruptRunError(
                f"run ledger accepted_commit is inconsistent with status: {path}"
            )

    def transition(
        self, run_id: str, target_stage: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Advance exactly one main stage, including a failed stage's target."""

        target = _validate_stage(target_stage)

        def apply(record: dict[str, Any], now: str) -> None:
            current = record["status"]
            self._require_mutable(current)
            failed_target = _failed_target(current)
            if failed_target is not None:
                expected = failed_target
            elif current in MAIN_STAGES:
                expected = _next_stage(current)
            else:  # Defensive: load() rejects this first.
                expected = None
            if target != expected:
                raise InvalidTransitionError(
                    f"illegal run transition from {current} to {target}; "
                    f"expected {expected or 'no further stage'}"
                )
            record["status"] = target
            record["error"] = None
            if MAIN_STAGES.index(target) >= MAIN_STAGES.index("COMMITTED"):
                record["accepted_commit"] = True
            self._append_history(
                record,
                from_stage=current,
                to_stage=target,
                time=now,
                reason=reason or f"transition to {target}",
            )

        return self._mutate(run_id, apply)

    def fail(
        self,
        run_id: str,
        target_stage: str,
        error: Any,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record failure while entering the one legal next/failed target stage."""

        target = _validate_stage(target_stage)
        if target == "NEW":
            raise InvalidTransitionError("NEW cannot be a failure target")
        _ensure_serializable(error, label="error")

        def apply(record: dict[str, Any], now: str) -> None:
            current = record["status"]
            self._require_mutable(current)
            prior_failed_target = _failed_target(current)
            if prior_failed_target is not None:
                expected = prior_failed_target
            elif current in MAIN_STAGES:
                expected = _next_stage(current)
            else:
                expected = None
            if target != expected:
                raise InvalidTransitionError(
                    f"cannot fail target {target} from {current}; "
                    f"expected {expected or 'no further stage'}"
                )

            retry_counts = record.setdefault("retry_counts", {})
            if not isinstance(retry_counts, dict):
                raise CorruptRunError("run ledger retry_counts is not an object")
            retry_count = int(retry_counts.get(target, 0)) + 1
            retry_counts[target] = retry_count
            failed_status = f"FAILED_{target}"
            record["status"] = failed_status
            record["error"] = self._error_payload(
                error, target=target, retry_count=retry_count, time=now
            )
            failure_reason = reason or self._error_reason(error, target)
            self._append_history(
                record,
                from_stage=current,
                to_stage=failed_status,
                time=now,
                reason=failure_reason,
            )
            record["history"][-1]["error"] = copy.deepcopy(record["error"])

        return self._mutate(run_id, apply)

    def resume(
        self, run_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Record a retry request while remaining at ``FAILED_<target>``.

        The only subsequent successful transition is the corresponding target
        stage.  A repeated failure must also name that same target.
        """

        def apply(record: dict[str, Any], now: str) -> None:
            current = record["status"]
            self._require_mutable(current)
            target = _failed_target(current)
            if target is None:
                raise InvalidTransitionError(
                    f"run cannot resume because status is not failed: {current}"
                )
            count = int(record.get("resume_count", 0)) + 1
            record["resume_count"] = count
            resume_reason = reason or f"retry {target}"
            record["resume"] = {
                "target_stage": target,
                "at": now,
                "count": count,
                "reason": resume_reason,
            }
            self._append_history(
                record,
                from_stage=current,
                to_stage=current,
                time=now,
                reason=resume_reason,
            )

        return self._mutate(run_id, apply)

    def abort(self, run_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Terminate a run without deleting accepted commits or other artifacts."""

        def apply(record: dict[str, Any], now: str) -> None:
            current = record["status"]
            self._require_mutable(current)
            accepted = bool(record.get("accepted_commit")) or self._is_after_commit(
                current
            )
            abort_reason = reason or "aborted"
            record["status"] = "ABORTED"
            record["accepted_commit"] = accepted
            record["abort"] = {
                "at": now,
                "reason": abort_reason,
                "accepted_commit": accepted,
                "artifacts_preserved": True,
            }
            self._append_history(
                record,
                from_stage=current,
                to_stage="ABORTED",
                time=now,
                reason=abort_reason,
            )

        return self._mutate(run_id, apply)

    def _mutate(
        self,
        run_id: str,
        operation: Callable[[dict[str, Any], str], None],
    ) -> dict[str, Any]:
        safe_id = _validate_run_id(run_id)
        _validate_project(self.project_root)
        record = copy.deepcopy(self.load(safe_id))
        now = _timestamp_after(record["updated_at"])
        operation(record, now)
        record["revision"] += 1
        record["updated_at"] = now
        path = self._ledger_path(safe_id)
        self._validate_record(record, expected_run_id=safe_id, path=path)
        _atomic_write_json(path, record)
        return copy.deepcopy(record)

    @staticmethod
    def _append_history(
        record: dict[str, Any],
        *,
        from_stage: str | None,
        to_stage: str,
        time: str,
        reason: str,
    ) -> None:
        if not isinstance(reason, str):
            raise RunStoreError("history reason must be a string")
        history = record.get("history")
        if not isinstance(history, list):
            raise CorruptRunError("run ledger history is not a list")
        history.append(
            {"from": from_stage, "to": to_stage, "time": time, "reason": reason}
        )

    @staticmethod
    def _require_mutable(status: str) -> None:
        if status in TERMINAL_STAGES:
            raise InvalidTransitionError(f"terminal run status cannot change: {status}")

    @staticmethod
    def _validate_positive_number(value: int | None, *, label: str) -> None:
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise RunStoreError(f"{label} must be a positive integer or null")

    @staticmethod
    def _error_payload(
        error: Any, *, target: str, retry_count: int, time: str
    ) -> dict[str, Any]:
        payload: dict[str, Any]
        if isinstance(error, Mapping):
            payload = copy.deepcopy(dict(error))
        elif isinstance(error, str):
            payload = {"message": error}
        else:
            payload = {"details": copy.deepcopy(error)}
        payload.update(
            {"target_stage": target, "retry_count": retry_count, "at": time}
        )
        return payload

    @staticmethod
    def _error_reason(error: Any, target: str) -> str:
        if isinstance(error, str) and error:
            return error
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return str(error["message"])
        return f"failed while entering {target}"

    @staticmethod
    def _is_after_commit(status: str) -> bool:
        if status in MAIN_STAGES:
            return MAIN_STAGES.index(status) >= MAIN_STAGES.index("COMMITTED")
        target = _failed_target(status)
        if target is None:
            return False
        # FAILED_X means the last accepted checkpoint is immediately before X.
        return MAIN_STAGES.index(target) - 1 >= MAIN_STAGES.index("COMMITTED")


__all__ = [
    "AtomicRunWriteError",
    "CorruptRunError",
    "InvalidRunIdError",
    "InvalidTransitionError",
    "MAIN_STAGES",
    "RunStore",
    "RunStoreError",
    "SCHEMA_VERSION",
    "TERMINAL_STAGES",
]

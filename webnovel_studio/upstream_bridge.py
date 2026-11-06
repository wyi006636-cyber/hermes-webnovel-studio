"""Compatibility bridge from the Hermes adapter to the vendored upstream CLI.

This module deliberately stops at the subprocess boundary.  It resolves and
validates the three roots, builds a minimal child environment, and invokes
upstream's single ``scripts/webnovel.py`` entry point without a shell, using
the interpreter Hermes itself runs on. It does not implement workflow state,
LLM behavior, or Hermes hooks.

The child process receives an allowlisted environment only (locale, paths and
temp directories plus the engine's own root variables). Hermes credentials and
other variables in the parent environment are never forwarded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT_ENV = "WEBNOVEL_PROJECT_ROOT"
PROJECT_MARKER = Path(".webnovel") / "state.json"
UPSTREAM_CLI = Path("scripts") / "webnovel.py"
VENDOR_DIR = Path("_vendor") / "webnovel-writer"

# Parent variables a Python child process legitimately needs. Everything else
# (API keys, tokens, Hermes settings) stays in the Hermes process.
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Windows process essentials.
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)


class BridgeError(RuntimeError):
    """Raised when the bridge cannot safely resolve or launch upstream."""


@dataclass(frozen=True)
class BridgeResult:
    """Stable, serialization-friendly view of an upstream process result."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether upstream exited successfully."""

        return self.returncode == 0

    def as_dict(self) -> dict[str, object]:
        """Return the result using only JSON-compatible value types."""

        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "ok": self.ok,
        }

    def raise_for_status(self) -> None:
        """Opt in to ``CalledProcessError`` for callers that require success."""

        if not self.ok:
            raise subprocess.CalledProcessError(
                self.returncode,
                list(self.command),
                output=self.stdout,
                stderr=self.stderr,
            )


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _normalize_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base if base is not None else Path.cwd()) / path
    return path.resolve(strict=False)


def _configured_path(
    explicit: str | os.PathLike[str] | None,
    environ: Mapping[str, str],
    variable: str,
) -> str | os.PathLike[str] | None:
    if explicit is not None:
        return explicit
    configured = environ.get(variable)
    return configured if configured else None


def _require_directory(path: Path, *, label: str) -> Path:
    if not path.exists():
        raise BridgeError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise BridgeError(f"{label} is not a directory: {path}")
    return path


def resolve_adapter_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the Hermes plugin directory.

    An explicit argument is accepted for dependency injection; otherwise the
    package directory is used. Code locations are never taken from the
    environment.
    """

    root = (
        _normalize_path(explicit)
        if explicit is not None
        else Path(__file__).resolve().parent
    )
    return _require_directory(root, label="adapter root")


def resolve_upstream_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve and validate the vendored Webnovel Writer engine root.

    The engine always ships inside the plugin (``_vendor/webnovel-writer``).
    An explicit argument is accepted for dependency injection only.
    """

    if explicit is not None:
        root = _normalize_path(explicit)
    else:
        root = (Path(__file__).resolve().parent / VENDOR_DIR).resolve()
    _require_directory(root, label="upstream root")
    cli = root / UPSTREAM_CLI
    if not cli.is_file():
        raise BridgeError(
            f"upstream root is missing unified CLI scripts/webnovel.py: {cli}"
        )
    return root


def _validate_project_root(root: Path) -> Path:
    _require_directory(root, label="project root")
    marker = root / PROJECT_MARKER
    if not marker.is_file():
        raise BridgeError(f"project root is missing .webnovel/state.json: {root}")
    return root


def resolve_project_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a novel project without consulting pointers or other projects.

    Precedence is explicit argument, ``WEBNOVEL_PROJECT_ROOT``, then an upward
    marker search from ``cwd`` (or the process working directory).  Relative
    configured paths are interpreted against that starting directory.  The
    result is always canonical and absolute.
    """

    env = _environment(environ)
    start = _normalize_path(cwd if cwd is not None else Path.cwd())
    configured = _configured_path(explicit, env, PROJECT_ROOT_ENV)
    if configured is not None:
        return _validate_project_root(_normalize_path(configured, base=start))

    _require_directory(start, label="current working directory")
    for candidate in (start, *start.parents):
        if (candidate / PROJECT_MARKER).is_file():
            return candidate.resolve()
    raise BridgeError(
        f"no .webnovel/state.json found from current directory or its parents: {start}"
    )


def resolve_python_executable(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Return the interpreter used for engine subprocesses.

    This is the interpreter Hermes runs on (``sys.executable``), which is where
    Hermes installs the plugin's declared ``python_dependencies``. An explicit
    path is accepted for dependency injection in tests.
    """

    raw = explicit if explicit is not None else sys.executable
    # Do not dereference a virtualenv's interpreter symlink: the stable contract
    # is to reuse the exact current ``sys.executable`` path.
    executable = Path(os.path.abspath(os.path.expanduser(os.fspath(raw))))
    if not executable.exists():
        raise BridgeError(f"Python executable does not exist: {executable}")
    if not executable.is_file():
        raise BridgeError(f"Python executable is not a file: {executable}")
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise BridgeError(f"Python executable is not executable: {executable}")
    return executable


def _default_data_root() -> Path:
    from .config import plugin_data_root

    return plugin_data_root()


def build_subprocess_env(
    *,
    adapter_root: str | os.PathLike[str],
    upstream_root: str | os.PathLike[str],
    project_root: str | os.PathLike[str],
    base_env: Mapping[str, str] | None = None,
    data_root: str | os.PathLike[str] | None = None,
    import_path: Sequence[str] | None = None,
) -> dict[str, str]:
    """Build the allowlisted child environment for the engine CLI.

    Only locale/path/temp variables are copied from ``base_env``; the engine's
    Claude-compatible root variables are pointed at the plugin, the project and
    the per-plugin data directory, and ``PYTHONPATH`` mirrors the parent's
    import path (``sys.path`` unless *import_path* is given).
    """

    source = _environment(base_env)
    adapter = resolve_adapter_root(adapter_root)
    upstream = resolve_upstream_root(upstream_root)
    project = resolve_project_root(project_root, environ={})
    engine_home = _normalize_path(data_root) if data_root is not None else _default_data_root()

    child = {
        key: value
        for key, value in source.items()
        if key in _CHILD_ENV_ALLOWLIST or key.startswith("LC_")
    }
    child.update(
        {
            "WEBNOVEL_HERMES_ROOT": str(adapter),
            "WEBNOVEL_UPSTREAM_ROOT": str(upstream),
            "WEBNOVEL_PROJECT_ROOT": str(project),
            "CLAUDE_PLUGIN_ROOT": str(upstream),
            "CLAUDE_PROJECT_DIR": str(project),
            "PYTHONIOENCODING": "utf-8",
            # Keep the installed plugin tree free of __pycache__ files.
            "PYTHONDONTWRITEBYTECODE": "1",
            "WEBNOVEL_CLAUDE_HOME": str(engine_home.resolve(strict=False)),
        }
    )
    inherited_path = _inherited_import_path(sys.path if import_path is None else import_path)
    if inherited_path:
        child["PYTHONPATH"] = inherited_path
    return child


def _inherited_import_path(entries: Sequence[str]) -> str:
    """Join the parent's import path so the child sees the same packages.

    Hermes may run on a shared interpreter and attach the dependency
    environment (where the plugin's ``python_dependencies`` are installed) to
    ``sys.path`` at startup rather than through the interpreter's own
    site-packages. Re-exporting the parent's import path is the only way for
    ``sys.executable`` children to import exactly what the plugin itself can.
    """

    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        if not entry or entry in seen or not os.path.exists(entry):
            continue
        seen.add(entry)
        kept.append(entry)
    return os.pathsep.join(kept)


class UpstreamBridge:
    """Validated subprocess client for upstream's unified CLI."""

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] | None = None,
        upstream_root: str | os.PathLike[str] | None = None,
        adapter_root: str | os.PathLike[str] | None = None,
        python_executable: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        data_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._base_env = dict(_environment(environ))
        self.adapter_root = resolve_adapter_root(adapter_root)
        self.upstream_root = resolve_upstream_root(upstream_root)
        self.project_root = resolve_project_root(
            project_root, cwd=cwd, environ=self._base_env
        )
        self.python_executable = resolve_python_executable(python_executable)
        self.cli_path = self.upstream_root / UPSTREAM_CLI
        self.child_env = build_subprocess_env(
            adapter_root=self.adapter_root,
            upstream_root=self.upstream_root,
            project_root=self.project_root,
            base_env=self._base_env,
            data_root=data_root,
        )

    def command(
        self,
        subcommand: str,
        *arguments: str | os.PathLike[str],
    ) -> list[str]:
        """Build the exact argv list used for an upstream invocation."""

        if not subcommand or subcommand.startswith("-"):
            raise BridgeError(f"invalid upstream subcommand: {subcommand!r}")
        return [
            str(self.python_executable),
            str(self.cli_path),
            "--project-root",
            str(self.project_root),
            subcommand,
            *(str(argument) for argument in arguments),
        ]

    def run(
        self,
        subcommand: str,
        *arguments: str | os.PathLike[str],
        timeout: float | None = None,
    ) -> BridgeResult:
        """Run upstream and return output even when its exit code is nonzero.

        Configuration errors are raised before spawn and OS spawn failures are
        wrapped with command context.  ``TimeoutExpired`` is intentionally
        preserved so callers retain its timeout and partial stdout/stderr fields.
        """

        command = self.command(subcommand, *arguments)
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                env=self.child_env,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except OSError as exc:
            raise BridgeError(
                "failed to spawn upstream command "
                f"(python={self.python_executable}, cli={self.cli_path}, "
                f"command={command!r}): {exc}"
            ) from exc
        return BridgeResult(
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def where(self, *, timeout: float | None = None) -> BridgeResult:
        return self.run("where", timeout=timeout)

    def preflight(
        self,
        *,
        output_format: str = "json",
        timeout: float | None = None,
    ) -> BridgeResult:
        if output_format not in {"text", "json"}:
            raise BridgeError(f"invalid preflight output format: {output_format!r}")
        return self.run("preflight", "--format", output_format, timeout=timeout)

    def doctor(
        self,
        *,
        output_format: str = "json",
        chapter: int | None = None,
        deep: bool = False,
        timeout: float | None = None,
    ) -> BridgeResult:
        if output_format not in {"text", "json"}:
            raise BridgeError(f"invalid doctor output format: {output_format!r}")
        arguments: list[str] = ["--format", output_format]
        if chapter is not None:
            if chapter < 1:
                raise BridgeError("doctor chapter must be at least 1")
            arguments.extend(["--chapter", str(chapter)])
        if deep:
            arguments.append("--deep")
        return self.run("doctor", *arguments, timeout=timeout)


__all__ = [
    "BridgeError",
    "BridgeResult",
    "UpstreamBridge",
    "build_subprocess_env",
    "resolve_adapter_root",
    "resolve_project_root",
    "resolve_python_executable",
    "resolve_upstream_root",
]

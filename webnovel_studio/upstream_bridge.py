"""Compatibility bridge from the Hermes adapter to the vendored upstream CLI.

This module deliberately stops at the subprocess boundary.  It resolves and
validates the three roots, builds an isolated compatibility environment, and
invokes upstream's single ``scripts/webnovel.py`` entry point without a shell.
It does not implement workflow state, LLM behavior, or Hermes hooks.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


ADAPTER_ROOT_ENV = "WEBNOVEL_HERMES_ROOT"
UPSTREAM_ROOT_ENV = "WEBNOVEL_UPSTREAM_ROOT"
PROJECT_ROOT_ENV = "WEBNOVEL_PROJECT_ROOT"
PROJECT_MARKER = Path(".webnovel") / "state.json"
UPSTREAM_CLI = Path("scripts") / "webnovel.py"
INSTALL_MARKER = ".webnovel-hermes-install.json"
INSTALL_MARKER_SCHEMA = "webnovel-hermes-install/v1"


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


def resolve_adapter_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the Hermes adapter/plugin directory.

    An explicit argument is accepted for dependency injection.  Otherwise
    ``WEBNOVEL_HERMES_ROOT`` wins over the package directory.
    """

    env = _environment(environ)
    configured = _configured_path(explicit, env, ADAPTER_ROOT_ENV)
    root = (
        _normalize_path(configured)
        if configured is not None
        else Path(__file__).resolve().parent
    )
    return _require_directory(root, label="adapter root")


def resolve_upstream_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve and validate the upstream Webnovel Writer plugin root."""

    env = _environment(environ)
    configured = _configured_path(explicit, env, UPSTREAM_ROOT_ENV)
    if configured is not None:
        root = _normalize_path(configured)
    else:
        package_root = Path(__file__).resolve().parent
        installed_vendor = package_root / "_vendor" / "webnovel-writer"
        repository_vendor = (
            package_root.parent
            / "upstream"
            / "webnovel-writer"
            / "webnovel-writer"
        )
        root = (
            installed_vendor if installed_vendor.is_dir() else repository_vendor
        ).resolve()
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


def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _installed_runtime_python(adapter_root: Path) -> Path | None:
    marker_path = adapter_root / INSTALL_MARKER
    if not marker_path.is_file() or marker_path.is_symlink():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("installed runtime marker is invalid") from exc
    if not isinstance(marker, dict) or marker.get("schema") != INSTALL_MARKER_SCHEMA:
        raise BridgeError("installed runtime marker is invalid")
    source = marker.get("source")
    runtime = marker.get("runtime_python")
    if runtime is None:
        return None
    if not isinstance(source, str) or not isinstance(runtime, str):
        raise BridgeError("installed runtime marker is invalid")
    expected = _venv_python(Path(source).expanduser().parent)
    candidate = Path(runtime).expanduser()
    if candidate != expected:
        raise BridgeError("installed runtime marker has an unexpected interpreter")
    return candidate


def resolve_python_executable(
    explicit: str | os.PathLike[str] | None = None,
    *,
    project_root: Path | None = None,
    adapter_root: Path | None = None,
) -> Path:
    """Resolve upstream Python, preferring managed project virtual environments."""

    if explicit is not None:
        raw = explicit
    else:
        candidates: list[Path] = []
        if project_root is not None:
            candidates.append(_venv_python(project_root))
        if adapter_root is not None:
            candidates.append(_venv_python(adapter_root.parent))
            installed = _installed_runtime_python(adapter_root)
            if installed is not None:
                candidates.append(installed)
        raw = next((candidate for candidate in candidates if candidate.exists()), sys.executable)
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


def build_subprocess_env(
    *,
    adapter_root: str | os.PathLike[str],
    upstream_root: str | os.PathLike[str],
    project_root: str | os.PathLike[str],
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child-only Claude/Hermes compatibility environment."""

    source = dict(_environment(base_env))
    adapter = resolve_adapter_root(adapter_root, environ={})
    upstream = resolve_upstream_root(upstream_root, environ={})
    project = resolve_project_root(project_root, environ={})

    hermes_home_raw = source.get("HERMES_HOME")
    if hermes_home_raw:
        hermes_home = _normalize_path(hermes_home_raw)
    else:
        home_raw = source.get("HOME")
        home = _normalize_path(home_raw) if home_raw else Path.home().resolve()
        hermes_home = home / ".hermes"

    source.update(
        {
            "WEBNOVEL_HERMES_ROOT": str(adapter),
            "WEBNOVEL_UPSTREAM_ROOT": str(upstream),
            "WEBNOVEL_PROJECT_ROOT": str(project),
            "CLAUDE_PLUGIN_ROOT": str(upstream),
            "CLAUDE_PROJECT_DIR": str(project),
            "PYTHONIOENCODING": "utf-8",
            "WEBNOVEL_CLAUDE_HOME": str(
                (hermes_home / "webnovel-writer").resolve(strict=False)
            ),
        }
    )
    return source


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
    ) -> None:
        self._base_env = dict(_environment(environ))
        self.adapter_root = resolve_adapter_root(
            adapter_root, environ=self._base_env
        )
        self.upstream_root = resolve_upstream_root(
            upstream_root, environ=self._base_env
        )
        self.project_root = resolve_project_root(
            project_root, cwd=cwd, environ=self._base_env
        )
        self.python_executable = resolve_python_executable(
            python_executable,
            project_root=self.project_root,
            adapter_root=self.adapter_root,
        )
        self.cli_path = self.upstream_root / UPSTREAM_CLI
        self.child_env = build_subprocess_env(
            adapter_root=self.adapter_root,
            upstream_root=self.upstream_root,
            project_root=self.project_root,
            base_env=self._base_env,
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

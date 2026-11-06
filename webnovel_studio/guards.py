"""Hermes ``pre_tool_call`` protection for Webnovel Writer project files.

The guard is intentionally lexical first and fail-closed for commands that
mention protected paths.  Canonical state must be mutated through the adapter's
runtime so that upstream's accepted commit chain remains authoritative.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

PathKind = Literal["canonical", "chapter", "ordinary"]

_WRITE_ALIASES = {"write_file", "write", "file_write"}
_PATCH_ALIASES = {"patch", "apply_patch", "edit_file", "edit"}
_EXEC_ALIASES = {
    "terminal",
    "shell",
    "bash",
    "run_command",
    "execute_command",
    "execute_code",
    "code_exec",
}
_PATH_KEYS = ("path", "file_path", "target", "filename")
_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.*?)\s*$",
    re.MULTILINE,
)
_PROTECTED_TOKEN_RE = re.compile(r"\.story-system|\.webnovel|正文", re.IGNORECASE)
_QUOTED_RE = re.compile(r"(['\"])(.*?)\1", re.DOTALL)
_UNQUOTED_PATH_RE = re.compile(
    r"[^\s'\"`|;&<>{}\[\](),]*?(?:\.story-system|\.webnovel|正文)"
    r"[^\s'\"`|;&<>{}\[\](),]*",
    re.IGNORECASE,
)
_OUTPUT_REDIRECT_RE = re.compile(
    r"(?<![<>=])(?:"
    r"(?:\d+)?<>|(?:\d+)?>\||&>>?|(?:\d+)?>&(?!\d|-)|(?:\d+)?>{1,2}"
    r")\s*(?P<path>(?!&(?:\d+|-)(?:\s|$))['\"][^'\"]+['\"]|"
    r"(?!&(?:\d+|-)(?:\s|$))[^\s;&|]+)"
)

_CANONICAL_MESSAGE = (
    "Blocked: canonical Webnovel state may only be changed through "
    "webnovel_runtime's accepted commit chain."
)
_CHAPTER_MESSAGE = (
    "Approval required: this operation would directly overwrite chapter text "
    "outside the webnovel_runtime commit chain."
)


def _normalize_tool_name(tool_name: object) -> str:
    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    return re.split(r"[./]", normalized)[-1]


def _strip_path_quotes(text: object) -> str:
    value = str(text).strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"`":
        value = value[1:-1].strip()
    return value


def _lexical_parts(text: object, cwd: object | None = None) -> tuple[str, ...]:
    value = _strip_path_quotes(text).replace("\\", "/")
    if not value:
        return ()

    # Keep Windows drive paths lexical on non-Windows hosts.  Relative paths are
    # joined to cwd before normalization so traversal cannot hide a marker.
    is_windows_absolute = bool(re.match(r"^[A-Za-z]:/", value))
    if not value.startswith("/") and not is_windows_absolute and cwd:
        base = _strip_path_quotes(cwd).replace("\\", "/")
        value = posixpath.join(base, value)
    value = posixpath.normpath(value)
    return tuple(part for part in value.split("/") if part not in {"", "."})


def _kind_from_parts(parts: tuple[str, ...]) -> PathKind:
    lowered = tuple(part.casefold() for part in parts)
    if ".story-system" in lowered:
        return "canonical"

    for index, part in enumerate(lowered):
        if part != ".webnovel":
            continue
        descendants = lowered[index + 1 :]
        if not descendants:
            # Replacing/removing the whole metadata root necessarily mutates
            # canonical state. Read-only commands are still allowed later.
            return "canonical"
        # Run-scoped contracts and temporary artifacts are adapter staging, not
        # canonical projections. They remain writable by isolated roles.
        if descendants and descendants[0] in {"runs", "tmp", "backups"}:
            continue
        for child in descendants:
            if (
                child == "state.json"
                or child.startswith("index.db")
                or child.startswith("memory")
                or child.startswith("project_memory")
                or child
                in {
                    "summaries",
                    "summary",
                    "vector",
                    "vectors",
                    "projection",
                    "projections",
                }
            ):
                return "canonical"

    if "正文" in parts:
        return "chapter"
    return "ordinary"


def _local_path(text: object, cwd: object | None) -> Path | None:
    value = _strip_path_quotes(text)
    if not value or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    value = os.path.expanduser(value)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(_strip_path_quotes(cwd) if cwd else os.getcwd()) / candidate
    return candidate


def _classify_path(text: object, cwd: object | None = None) -> PathKind:
    """Classify a path lexically, then through existing symlink parents."""
    lexical = _kind_from_parts(_lexical_parts(text, cwd))
    if lexical == "canonical":
        return lexical

    candidate = _local_path(text, cwd)
    physical: PathKind = "ordinary"
    if candidate is not None:
        try:
            if candidate.exists() or candidate.parent.exists():
                physical = _kind_from_parts(tuple(candidate.resolve(strict=False).parts))
        except (OSError, RuntimeError):
            # Lexical classification remains authoritative if the filesystem
            # cannot safely resolve the candidate.
            pass

    if physical == "canonical":
        return physical
    if lexical == "chapter" or physical == "chapter":
        return "chapter"
    return "ordinary"


def _cwd(args: Mapping[str, Any], kwargs: Mapping[str, Any]) -> object | None:
    return args.get("cwd") or args.get("workdir") or kwargs.get("cwd") or kwargs.get("workdir")


def _result_for_kinds(kinds: list[PathKind]) -> dict[str, str] | None:
    if "canonical" in kinds:
        return {"action": "block", "message": _CANONICAL_MESSAGE}
    if "chapter" in kinds:
        return {
            "action": "approve",
            "message": _CHAPTER_MESSAGE,
            "rule_key": "webnovel:chapter-overwrite",
        }
    return None


def _direct_paths(args: Mapping[str, Any]) -> list[object]:
    return [args[key] for key in _PATH_KEYS if args.get(key) not in (None, "")]


def _patch_paths(args: Mapping[str, Any]) -> list[object]:
    paths = _direct_paths(args)
    patch_text = args.get("patch")
    if patch_text is None:
        patch_text = args.get("patch_text")
    if patch_text is not None:
        paths.extend(match for match in _PATCH_FILE_RE.findall(str(patch_text)) if match)
    return paths


def _command_text(args: Mapping[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code"):
        value = args.get(key)
        if value not in (None, ""):
            if isinstance(value, (list, tuple)):
                return " ".join(str(item) for item in value)
            return str(value)
    return ""


def _protected_command_kinds(command: str, cwd: object | None) -> list[PathKind]:
    candidates: list[str] = []
    candidates.extend(
        value for _, value in _QUOTED_RE.findall(command) if _PROTECTED_TOKEN_RE.search(value)
    )
    candidates.extend(_UNQUOTED_PATH_RE.findall(command))
    try:
        for token in shlex.split(command, posix=True):
            # Classify every concrete token so an innocuous-looking symlink
            # alias cannot hide a canonical destination from shell tools.
            candidates.append(token)
            if "=" in token:
                value = token.split("=", 1)[1]
                if value:
                    candidates.append(value)
    except ValueError:
        pass

    kinds: list[PathKind] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            kind = _classify_path(candidate, cwd)
            if kind != "ordinary":
                kinds.append(kind)
    return kinds


def _has_write_indicator(command: str) -> bool:
    if _OUTPUT_REDIRECT_RE.search(command):
        return True
    if re.search(
        r"(?<![\w.-])(?:rm|mv|cp|touch|truncate|chmod|chown|install|tee)(?![\w.-])",
        command,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"(?:^|\s)(?:sed|perl)(?:\s+[^\n;&|]*)?\s-[A-Za-z]*i\b", command, re.I):
        return True
    if re.search(r"\bdd\b[^\n;&|]*\bof\s*=", command, re.I):
        return True
    if re.search(
        r"\bfind\b[^\n;&|]*(?:-delete|-exec(?:dir)?|-ok(?:dir)?|-fprint0?|-fprintf|-fls)\b",
        command,
        re.I,
    ):
        return True
    if re.search(r"\b(?:sed|perl)\b[^\n;&|]*--in-place(?:=|\s|$)", command, re.I):
        return True
    if re.search(r"(?:^|\s)--output(?:=|\s+)", command, re.I):
        return True
    if re.search(
        r"\.(?:write_text|write_bytes|unlink|remove|rename|replace)\s*\(", command
    ):
        return True
    if re.search(r"\bshutil\.(?:copy|copy2|copyfile|copytree|move)\s*\(", command):
        return True
    if re.search(
        r"\bopen\s*\([^)]*(?:(?:,|mode\s*=)\s*['\"][wax](?:[+bt]*)['\"])",
        command,
        re.IGNORECASE | re.DOTALL,
    ):
        return True
    return False


def _copy_policy(
    command: str,
    cwd: object | None,
) -> tuple[bool, dict[str, str] | None]:
    """Analyze a simple cp, including redirections and effective destinations."""
    redirect_paths = [
        match.group("path") for match in _OUTPUT_REDIRECT_RE.finditer(command)
    ]
    if any(re.search(r"[$`*?\[]", value) for value in redirect_paths):
        return False, None
    redirect_kinds: list[PathKind] = [
        _classify_path(value, cwd) for value in redirect_paths if value not in {"&1", "&2"}
    ]
    redirect_result = _result_for_kinds(redirect_kinds)
    if redirect_result is not None:
        return True, redirect_result

    cleaned_command = _OUTPUT_REDIRECT_RE.sub(" ", command)
    segments = _shell_segments(cleaned_command)
    if len(segments) != 1:
        return False, None
    executable, arguments = _segment_command(segments[0])
    if executable != "cp" or not arguments:
        return False, None

    operands: list[str] = []
    after_options = False
    for argument in arguments:
        if argument == "--":
            after_options = True
        elif not after_options and (
            argument in {"-t", "--target-directory"}
            or argument.startswith("--target-directory=")
        ):
            # Destination-first forms are intentionally fail-closed.
            return False, None
        elif not after_options and argument.startswith("-"):
            continue
        else:
            operands.append(argument)
    if len(operands) < 2 or any(
        re.search(r"[$`*?\[]", operand) for operand in operands
    ):
        return False, None

    sources = operands[:-1]
    destination = operands[-1]
    destination_local = _local_path(destination, cwd)
    destination_is_directory = (
        len(sources) > 1
        or destination.replace("\\", "/").endswith("/")
        or bool(destination_local and destination_local.is_dir())
    )

    if destination_is_directory:
        destinations: list[object] = []
        for source in sources:
            basename = posixpath.basename(source.replace("\\", "/").rstrip("/"))
            if not basename:
                return False, None
            if destination_local is not None:
                destinations.append(destination_local / basename)
            else:
                destinations.append(posixpath.join(destination, basename))
    else:
        destinations = [destination]

    destination_kinds: list[PathKind] = [
        _classify_path(value, cwd) for value in destinations
    ]
    return True, _result_for_kinds(destination_kinds)


def _is_staging_path(value: object, cwd: object | None) -> bool:
    def contains_staging(parts: tuple[str, ...]) -> bool:
        lowered = tuple(part.casefold() for part in parts)
        return any(
            part == ".webnovel"
            and index + 1 < len(lowered)
            and lowered[index + 1] in {"runs", "tmp", "backups"}
            for index, part in enumerate(lowered)
        )

    if contains_staging(_lexical_parts(value, cwd)):
        return True
    candidate = _local_path(value, cwd)
    if candidate is not None:
        try:
            return contains_staging(tuple(candidate.resolve(strict=False).parts))
        except (OSError, RuntimeError):
            pass
    return False


def _project_root_from_cwd(cwd: object | None) -> Path | None:
    candidate = _local_path(".", cwd)
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve(strict=False)
        for directory in (resolved, *resolved.parents):
            if (directory / ".story-system").exists() or (directory / ".webnovel").exists():
                return directory
    except (OSError, RuntimeError):
        pass
    return None


def _path_contains_canonical(value: object, cwd: object | None) -> bool:
    candidate = _local_path(value, cwd)
    if candidate is None:
        return False
    try:
        resolved = candidate.resolve(strict=False)
        if not resolved.is_dir():
            return False
        project_root = _project_root_from_cwd(cwd)
        if project_root is not None:
            try:
                project_root.relative_to(resolved)
                return True
            except ValueError:
                pass
        return (resolved / ".story-system").exists() or (resolved / ".webnovel").exists()
    except (OSError, RuntimeError):
        return False


def _has_find_tree_mutation(command: str) -> bool:
    return bool(
        re.search(
            r"(?:^|\s)(?:-delete|-exec(?:dir)?|-ok(?:dir)?)(?:\s|$)",
            command,
            re.I,
        )
    )


def _dangerous_find_traversal(command: str, cwd: object | None) -> bool:
    """Detect destructive find operations that can reach canonical state."""
    for segment in _shell_segments(command):
        executable, arguments = _segment_command(segment)
        if executable != "find" or not _has_find_tree_mutation(segment):
            continue
        follows_links = "-L" in arguments or "-follow" in arguments
        targets: list[str] = []
        for argument in arguments:
            if argument in {"-H", "-L", "-P"}:
                continue
            if argument == "!" or argument.startswith("-") or argument.startswith("("):
                break
            targets.append(argument)
        if not targets:
            targets = ["."]
        for target in targets:
            if _path_contains_canonical(target, cwd):
                return True
            if follows_links and _is_staging_path(target, cwd):
                return True
    return False


def _dangerous_ancestor_mutation(command: str, cwd: object | None) -> bool:
    """Block recursive/destructive tools aimed at an ancestor project path."""
    for segment in _shell_segments(command):
        executable, arguments = _segment_command(segment)
        if executable not in {"rm", "mv", "chmod", "chown"}:
            continue
        for argument in arguments:
            if argument == "--" or argument.startswith("-"):
                continue
            if re.search(r"[*?\[]", argument):
                prefix = re.split(r"[*?\[]", argument, maxsplit=1)[0]
                probe = prefix.rstrip("/\\") or "."
                if _path_contains_canonical(probe, cwd):
                    return True
                continue
            if _path_contains_canonical(argument, cwd):
                return True
    return False


def _shell_segments(command: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"(?:&&|\|\||[;|])", command) if segment.strip()]


def _segment_command(segment: str) -> tuple[str, list[str]]:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return "", []
    while tokens and ("=" in tokens[0] and not tokens[0].startswith(("/", "./"))):
        tokens.pop(0)
    while tokens and Path(tokens[0]).name.casefold() in {"env", "command", "sudo"}:
        tokens.pop(0)
    if not tokens:
        return "", []
    return Path(tokens[0]).name.casefold(), tokens[1:]


def _is_python_read_only(command: str, tool: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    executable = Path(tokens[0]).name.casefold() if tokens else ""
    is_python = tool in {"execute_code", "code_exec"} or executable.startswith("python")
    if not is_python:
        return False
    if not re.search(r"\.read_(?:text|bytes)\s*\(|\bopen\s*\(", command):
        return False

    for match in re.finditer(r"\bopen\s*\((.*?)\)", command, re.DOTALL):
        call = match.group(1)
        mode_match = re.search(r"(?:,|mode\s*=)\s*['\"]([^'\"]+)['\"]", call)
        if mode_match and mode_match.group(1).replace("b", "").replace("t", "") not in {"r", "r+"}:
            return False
        if mode_match and "+" in mode_match.group(1):
            return False
    return True


def _is_explicitly_read_only(command: str, tool: str) -> bool:
    if _is_python_read_only(command, tool):
        return True

    read_commands = {"cat", "head", "tail", "less", "more", "grep", "rg", "find", "ls", "stat", "wc", "file", "jq"}
    git_read_commands = {"diff", "status", "show", "log", "grep", "ls-files"}
    segments = _shell_segments(command)
    if not segments:
        return False
    for segment in segments:
        executable, arguments = _segment_command(segment)
        if executable in read_commands:
            continue
        if executable == "sed" and not any(re.fullmatch(r"-[A-Za-z]*i.*", arg) for arg in arguments):
            continue
        if executable == "git":
            subcommand = next((arg for arg in arguments if not arg.startswith("-")), "")
            if subcommand in git_read_commands:
                continue
        return False
    return True


def pre_tool_call_guard(
    tool_name: str,
    args: Mapping[str, Any] | None,
    **kwargs: Any,
) -> None | dict[str, str]:
    """Block canonical-state writes and request approval for chapter writes."""
    tool = _normalize_tool_name(tool_name)
    if tool == "webnovel_runtime":
        return None

    arguments: Mapping[str, Any] = args if isinstance(args, Mapping) else {}
    cwd = _cwd(arguments, kwargs)

    if tool in _WRITE_ALIASES:
        return _result_for_kinds([_classify_path(path, cwd) for path in _direct_paths(arguments)])

    if tool in _PATCH_ALIASES:
        return _result_for_kinds([_classify_path(path, cwd) for path in _patch_paths(arguments)])

    if tool in _EXEC_ALIASES:
        command = _command_text(arguments)
        if _dangerous_find_traversal(command, cwd):
            return {"action": "block", "message": _CANONICAL_MESSAGE}
        if _dangerous_ancestor_mutation(command, cwd):
            return {"action": "block", "message": _CANONICAL_MESSAGE}
        is_copy, copy_result = _copy_policy(command, cwd)
        if is_copy:
            return copy_result
        kinds = _protected_command_kinds(command, cwd)
        if not kinds:
            return None
        if _has_write_indicator(command):
            return _result_for_kinds(kinds)
        if _is_explicitly_read_only(command, tool):
            return None
        # A protected path in an unknown command is treated as a write.
        return _result_for_kinds(kinds)

    return None


__all__ = ["pre_tool_call_guard"]

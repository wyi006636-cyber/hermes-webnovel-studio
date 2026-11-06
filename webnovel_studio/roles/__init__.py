"""Safe loader for the fixed Webnovel Writer role prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Final


ROLE_PROMPT_NAMES: Final = (
    "architect",
    "context-curator",
    "writer",
    "reader-editor",
)
_ROLE_FILES: Final = {
    "architect": "architect.md",
    "context-curator": "context-curator.md",
    "writer": "writer.md",
    "reader-editor": "reader-editor.md",
}
_ROLE_DIRECTORY: Final = Path(__file__).resolve().parent


def load_role_prompt(name: str) -> str:
    """Load one packaged UTF-8 prompt by its exact fixed role name.

    The caller cannot provide a path, extension, alias, or differently-cased
    name.  Mapping before filesystem access keeps traversal outside the loader's
    input language.
    """

    if not isinstance(name, str):
        raise ValueError("role prompt name must be a string")
    try:
        filename = _ROLE_FILES[name]
    except KeyError:
        raise KeyError(f"unknown role prompt: {name!r}") from None

    path = _ROLE_DIRECTORY / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read packaged role prompt: {name}") from exc


__all__ = ["ROLE_PROMPT_NAMES", "load_role_prompt"]

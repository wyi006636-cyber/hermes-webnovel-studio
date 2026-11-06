"""Hermes plugin registration for Webnovel Studio."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from .dashboard_service import SessionContextInjector
from .guards import pre_tool_call_guard
from .runtime import runtime_handler
from .tool_schema import WEBNOVEL_RUNTIME_ACTIONS, WEBNOVEL_RUNTIME_SCHEMA


# Backward-compatible import name; the values live only in tool_schema.py.
ACTIONS = WEBNOVEL_RUNTIME_ACTIONS
_SKILL_NAMES = (
    "webnovel-init",
    "webnovel-plan",
    "webnovel-write",
    "webnovel-review",
    "webnovel-query",
    "webnovel-learn",
    "webnovel-dashboard",
    "webnovel-doctor",
)


_SESSION_CONTEXT = SessionContextInjector()


def _pre_llm_call_session_context(
    *,
    session_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    return _SESSION_CONTEXT.context(session_id=session_id)


def _setup_webnovel_cli(parser: ArgumentParser) -> None:
    """Configure the top-level ``hermes webnovel`` operator surface."""
    parser.description = (
        "Webnovel Studio operator CLI. The writing workflow itself runs inside "
        "Hermes chat through the /webnovel-* skills."
    )
    parser.set_defaults(_webnovel_parser=parser)
    commands = parser.add_subparsers(dest="webnovel_command")
    live = commands.add_parser(
        "smoke-live",
        help="run the real-model novel workflow acceptance",
    )
    live.add_argument(
        "--project-root",
        type=Path,
        required=True,
        help="fresh absent path for the acceptance project",
    )


def _handle_webnovel_cli(args: Namespace) -> None:
    """Print usage when no operator subcommand was given."""
    parser = getattr(args, "_webnovel_parser", None)
    if parser is not None:
        parser.print_help(sys.stderr)
    else:
        print("usage: hermes webnovel smoke-live --project-root PATH", file=sys.stderr)
    raise SystemExit(2)


def register(ctx: Any) -> None:
    """Register the runtime tool plus the planned hook and CLI surfaces."""

    llm = getattr(ctx, "llm", None)

    def bound_runtime_handler(args: Any = None, **kwargs: Any) -> str:
        result = runtime_handler(args, llm=llm, **kwargs)
        if isinstance(args, dict):
            session_id = kwargs.get("session_id")
            project_root = args.get("project_root")
            if isinstance(session_id, str) and isinstance(project_root, str):
                _SESSION_CONTEXT.bind(
                    session_id=session_id,
                    project_root=project_root,
                )
        return result

    def bound_cli_handler(args: Namespace) -> None:
        if getattr(args, "webnovel_command", None) != "smoke-live":
            _handle_webnovel_cli(args)
            return
        if llm is None:
            print(
                json.dumps({"success": False, "error": "llm_unavailable"}),
                file=sys.stderr,
            )
            raise SystemExit(1)
        from .live_smoke import LiveAcceptanceError, run_live_acceptance

        try:
            result = run_live_acceptance(args.project_root, llm=llm)
        except (LiveAcceptanceError, OSError, ValueError) as exc:
            print(
                json.dumps(
                    {"success": False, "error": str(exc)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    ctx.register_tool(
        name="webnovel_runtime",
        toolset="webnovel",
        schema=WEBNOVEL_RUNTIME_SCHEMA,
        handler=bound_runtime_handler,
        description="Long-form novel runtime: initialize, plan, write, review and query a novel project.",
        emoji="📚",
    )
    skills_root = Path(__file__).resolve().parent / "skills"
    for skill_name in _SKILL_NAMES:
        ctx.register_skill(
            name=skill_name,
            path=skills_root / skill_name / "SKILL.md",
            description=f"Webnovel Studio workflow: {skill_name}",
        )
    ctx.register_hook("pre_tool_call", pre_tool_call_guard)
    ctx.register_hook("pre_llm_call", _pre_llm_call_session_context)
    ctx.register_cli_command(
        name="webnovel",
        help="Webnovel Studio operations",
        setup_fn=_setup_webnovel_cli,
        handler_fn=bound_cli_handler,
        description=(
            "Webnovel Studio operator CLI (real-model acceptance run)."
        ),
    )


__all__ = [
    "ACTIONS",
    "WEBNOVEL_RUNTIME_ACTIONS",
    "WEBNOVEL_RUNTIME_SCHEMA",
    "pre_tool_call_guard",
    "register",
]

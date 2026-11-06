"""Single source of truth for the ``webnovel_runtime`` tool contract."""

from __future__ import annotations

from typing import Any


WEBNOVEL_RUNTIME_ACTIONS = [
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

WEBNOVEL_RUNTIME_SCHEMA: dict[str, Any] = {
    "name": "webnovel_runtime",
    "description": (
        "Create, plan, write, review, and inspect a novel project. The write_chapter "
        "action produces one complete chapter in a single runtime call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": WEBNOVEL_RUNTIME_ACTIONS,
                "description": "Workflow action to execute.",
            },
            "project_root": {
                "type": "string",
                "description": "Absolute path to the novel project root.",
            },
            "run_id": {
                "type": "string",
                "description": "Existing resumable run identifier, when applicable.",
            },
            "chapter": {
                "type": "integer",
                "minimum": 1,
                "description": "One-based chapter number.",
            },
            "volume": {
                "type": "integer",
                "minimum": 1,
                "description": "One-based volume number.",
            },
            "mode": {
                "type": "string",
                "description": "Optional action-specific execution mode.",
            },
            "payload": {
                "type": "object",
                "description": "Optional action-specific structured input.",
                "additionalProperties": True,
            },
        },
        "required": ["action", "project_root"],
        "additionalProperties": False,
    },
}


__all__ = ["WEBNOVEL_RUNTIME_ACTIONS", "WEBNOVEL_RUNTIME_SCHEMA"]

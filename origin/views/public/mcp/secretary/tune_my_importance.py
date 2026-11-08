"""`tune_my_importance` (MIP-1087): MCP-only, see `secretary/__init__.py`.

How much each signal counts when Genos works out what matters to the member. Stored until they
change it back; nothing about any task changes.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import importance


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    levels = importance.set_levels(ctx, args.get("weights"), args.get("reset"))
    changed = ", ".join(f"{k} {v}" for k, v in levels.items()) or "all normal"
    return {
        "tuning": [
            {"signal": name, "level": levels.get(name, importance.DEFAULT_LEVEL)}
            for name in importance.SIGNALS
        ],
        "__summary__": f"Importance tuning: {changed}.",
    }


TUNE_MY_IMPORTANCE = Tool(
    name="tune_my_importance",
    description="Change how much each signal counts for what matters to the member.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "weights": {
                "type": "OBJECT",
                "properties": {
                    name: {
                        "type": "STRING",
                        "enum": list(importance.LEVELS),
                        "description": importance.SIGNAL_WORDS[name],
                    }
                    for name in importance.SIGNALS
                },
                "description": "Signals to change, each to off, low, normal (default) or high.",
            },
            "reset": {
                "type": "BOOLEAN",
                "description": "true puts every signal back to normal before `weights` apply.",
            },
        },
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)

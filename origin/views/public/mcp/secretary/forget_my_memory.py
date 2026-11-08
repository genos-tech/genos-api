"""`forget_my_memory` (MIP-1088): MCP-only, see `secretary/__init__.py`.

Deletes what Genos stored about the member from their activity. A destructive tool: it needs the
key's destructive opt-in, and it is announced as one. The logic is in
`origin/services/memory_control.py`; the Settings page deletes with the same function.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.services import memory_control


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    what = args.get("what")
    if not isinstance(what, str) or what not in memory_control.WHAT:
        raise ToolError(f"`what` must be one of: {', '.join(memory_control.WHAT)}.")
    out = memory_control.forget(ctx.user_id, what)
    gone = ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in out["deleted"].items())
    return {
        "forgotten": what,
        "deleted": out["deleted"],
        "stored": out["stored"],
        "__summary__": f"Deleted ({what}): {gone}.",
    }


FORGET_MY_MEMORY = Tool(
    name="forget_my_memory",
    description="Delete what Genos stored about the member from their activity.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "what": {
                "type": "STRING",
                "enum": list(memory_control.WHAT),
                "description": (
                    "importance: marks and tuning. summaries: the per-day activity counts. "
                    "events: what their AI apps opened. everything: all three."
                ),
            },
        },
        "required": ["what"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)

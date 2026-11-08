"""`get_my_memory` (MIP-1088): MCP-only, see `secretary/__init__.py`.

What Genos has stored about the member in the secretary work, in counts and dates, and whether
it is recording. Read-only; the logic is in `origin/services/memory_control.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import importance, memory_control


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    stored = memory_control.stored(ctx.user_id)
    enabled = importance.enabled(ctx.user_id)
    return {
        "recording": enabled,
        "stored": stored,
        "delete_with": "forget_my_memory",
        "pause_in": "Settings, What Genos remembers (turning recording off deletes all of this)",
        "__summary__": (
            f"Recording is {'on' if enabled else 'off'}; "
            f"{stored['ledger']['entries']} ledger entries, {stored['summaries']['rows']} "
            f"per-day rows, {stored['importance']['marks']} marks, "
            f"{stored['importance']['tuned_signals']} tuned signals."
        ),
    }


GET_MY_MEMORY = Tool(
    name="get_my_memory",
    description="Read what Genos has stored about the member from their activity, and whether it records.",
    parameters_schema={"type": "OBJECT", "properties": {}},
    run=_run,
    mcp_only=True,
)

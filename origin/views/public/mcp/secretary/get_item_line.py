"""`get_item_line` (MIP-1083): MCP-only, see `secretary/__init__.py`.

One task's LINE: everything that happened around it, from every source, in time order,
and the lines of the items next to it. The logic is in `origin/services/item_lines.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import item_lines


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    since, until = item_lines.resolve_window(ctx.user_id, args.get("since"), args.get("until"))
    include_related = args.get("include_related")
    related = item_lines.RELATED_MAX if include_related in (None, True) else 0
    return item_lines.item_lines(
        ctx,
        args.get("task_id"),
        since,
        until,
        limit=args.get("limit") or item_lines.DEFAULT_LIMIT,
        related_limit=related,
        include_unattached=args.get("include_unattached") in (None, True),
    )


GET_ITEM_LINE = Tool(
    name="get_item_line",
    description="Read one task's line: its events from every source in time order.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task."},
            "since": {
                "type": "STRING",
                "description": (
                    "An ISO date-time to start from (included), in the member's time zone "
                    "if it has no offset. Default: seven days before `until`. At most 31 "
                    "days back."
                ),
            },
            "until": {
                "type": "STRING",
                "description": "An ISO date-time to stop at (excluded). Default: now.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many of the newest events to tell. Default 50, at most 100.",
            },
            "include_related": {
                "type": "BOOLEAN",
                "description": "Also give the lines of the neighbouring items. Default true.",
            },
            "include_unattached": {
                "type": "BOOLEAN",
                "description": (
                    "Also give `unattached`, the member's own events in the window that belong "
                    "to no item. It is the same for every task, so when you read several tasks "
                    "of one window, ask for it once and pass false for the rest. Default true."
                ),
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    mcp_only=True,
)

"""`list_my_worklog` (MIP-1071): MCP-only, see `secretary/__init__.py`.

The member's own worklog for a day, a week or a range. The logic is in
`origin/services/worklog.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import worklog


def _list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return worklog.list_entries(
        ctx,
        window=args.get("window"),
        from_date=args.get("from_date"),
        to_date=args.get("to_date"),
        kind=args.get("kind"),
        review_state=args.get("review_state"),
        status=args.get("status"),
        limit=args.get("limit"),
    )


LIST_MY_WORKLOG = Tool(
    name="list_my_worklog",
    description="The member's own worklog for a day, a week or a range.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "window": {
                "type": "STRING",
                "enum": ["today", "yesterday", "week"],
                "description": "Default today. `week` is the last seven days, today included.",
            },
            "from_date": {
                "type": "STRING",
                "description": "Start of a range, YYYY-MM-DD, in the member's time zone.",
            },
            "to_date": {
                "type": "STRING",
                "description": "End of a range, inclusive. Without `from_date`, just that day.",
            },
            "kind": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": list(worklog.KINDS)},
                "description": "Only items of these kinds.",
            },
            "review_state": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": list(worklog.REVIEW_STATES)},
                "description": "Default suggested and accepted. Ask for `rejected` to see what was thrown out.",
            },
            "status": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": list(worklog.STATUSES)},
                "description": "Only open or only resolved items.",
            },
            "limit": {
                "type": "INTEGER",
                "description": f"Entries to return, 1 to {worklog.MAX_LIMIT}. Default {worklog.DEFAULT_LIMIT}.",
            },
        },
    },
    run=_list,
    mcp_only=True,
)

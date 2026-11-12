"""`list_my_unread` (MIP-1375): MCP-only, see `secretary/__init__.py`.

What the member has not read yet, as raw data plus stats: for "what happened while I was
away". Read-only, and it summarizes nothing: the model that asked does that. The logic is in
`origin/services/unread_digest.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import unread_digest


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return unread_digest.build_unread(
        ctx,
        since=args.get("since"),
        limit=args.get("limit"),
        kinds=args.get("kinds"),
        chat_id=args.get("chat_id"),
        task_id=args.get("task_id"),
    )


LIST_MY_UNREAD = Tool(
    name="list_my_unread",
    description="List what the member has not read yet: raw items and events, plus counts.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "since": {
                "type": "STRING",
                "description": (
                    "An ISO date-time to look back to, in the member's time zone if it has no "
                    "offset. Default: the last 30 days. At most 31 days back."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (
                    f"How many events to return across all items. Default "
                    f"{unread_digest.DEFAULT_LIMIT}, at most {unread_digest.MAX_LIMIT}."
                ),
            },
            "kinds": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": list(unread_digest.PLACES)},
                "description": "Only these places: dm, gm, mdm, pm (chats), task or note.",
            },
            "chat_id": {
                "type": "STRING",
                "description": "Only this chat's unread (a chat id from an earlier result).",
            },
            "task_id": {"type": "INTEGER", "description": "Only this task's unread."},
        },
    },
    run=_run,
    mcp_only=True,
)

"""`get_my_briefing` (MIP-1072): MCP-only, see `secretary/__init__.py`.

One call that catches the member up and helps them plan: what others changed on their
things, what waits on them and what they wait on, today's agenda, their worklog, and
the few things to look at first, with why. The logic is in `origin/services/briefing.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import briefing


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return briefing.build_briefing(
        ctx,
        window=args.get("window"),
        since=args.get("since"),
        project_id=args.get("project_id"),
    )


GET_MY_BRIEFING = Tool(
    name="get_my_briefing",
    description="Catch the member up and help them plan their day.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "window": {
                "type": "STRING",
                "enum": list(briefing.WINDOWS),
                "description": (
                    "since_last (default): what changed since the member last asked for a "
                    "briefing. today: since local midnight. week: the last seven days."
                ),
            },
            "since": {
                "type": "STRING",
                "description": (
                    "An ISO date-time to look back to instead, in the member's time zone "
                    "if it has no offset. At most 31 days back."
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Only look at tasks of this project (one the member is in). Chats and "
                    "notes belong to no project and are left out."
                ),
            },
        },
    },
    run=_run,
    mcp_only=True,
)

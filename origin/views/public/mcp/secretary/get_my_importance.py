"""`get_my_importance` (MIP-1087): MCP-only, see `secretary/__init__.py`.

What matters to the member right now, worked out from their own activity, with the reason for
each item, plus how it is tuned and what they marked. Read-only. The logic is in
`origin/services/importance.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import importance


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return importance.describe(ctx, limit=args.get("limit"), project_id=args.get("project_id"))


GET_MY_IMPORTANCE = Tool(
    name="get_my_importance",
    description="Read what matters most to the member now, from their own activity.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "limit": {
                "type": "INTEGER",
                "description": "How many top items to give. Default and most: 10.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "Only items of this project (one the member is in).",
            },
        },
    },
    run=_run,
    mcp_only=True,
)

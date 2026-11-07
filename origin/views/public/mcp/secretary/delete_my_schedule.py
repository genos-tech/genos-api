"""`delete_my_schedule` (MIP-1151): MCP-only, see `secretary/__init__.py`."""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import schedules
from origin.views.public.mcp.secretary._schedule_args import guarded


def _delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    guarded(lambda: schedules.delete(ctx, args.get("schedule_id")))
    return {"deleted": True, "__summary__": "Schedule deleted."}


DELETE_MY_SCHEDULE = Tool(
    name="delete_my_schedule",
    description="Delete one of the member's schedules.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "schedule_id": {"type": "STRING", "description": "From `list_my_schedules`."},
        },
        "required": ["schedule_id"],
    },
    run=_delete,
    requires_approval=True,
    mcp_only=True,
)

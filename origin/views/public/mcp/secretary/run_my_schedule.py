"""`run_my_schedule` (MIP-1151): MCP-only, see `secretary/__init__.py`."""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import schedules
from origin.views.public.mcp.secretary._schedule_args import guarded


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    out = guarded(lambda: schedules.run_now(ctx, args.get("schedule_id")))
    if out["status"] == "ok":
        summary = f"Ran: {out['title']} ({out['items']} item(s)). The report is in the Inbox."
    else:
        summary = f"The run failed: {out['error'] or out['status']}."
    return {**out, "__summary__": summary}


RUN_MY_SCHEDULE = Tool(
    name="run_my_schedule",
    description="Run one of the member's schedules once, now.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "schedule_id": {"type": "STRING", "description": "From `list_my_schedules`."},
        },
        "required": ["schedule_id"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)

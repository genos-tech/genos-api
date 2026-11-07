"""`list_my_schedules` (MIP-1151): MCP-only, see `secretary/__init__.py`.

The member's own schedules and the jobs a schedule can run. The logic is in
`origin/services/schedules.py`; the wording is in `origin/services/schedule_text.py`.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import schedule_text, schedules
from origin.services.user_time import user_zone


def _list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    user = CustomUser.objects.get(pk=ctx.user_id)
    zone = user_zone(user)
    rows = [schedule_text.present(s, zone) for s in schedules.list_schedules(ctx)]
    return {
        "time_zone": zone.key,
        "schedules": rows,
        "jobs": schedule_text.job_catalog(),
        "limit": schedules.MAX_SCHEDULES_PER_MEMBER,
        "__summary__": f"{len(rows)} schedule(s).",
    }


LIST_MY_SCHEDULES = Tool(
    name="list_my_schedules",
    description="The member's own schedules and the jobs a schedule can run.",
    parameters_schema={"type": "OBJECT", "properties": {}},
    run=_list,
    mcp_only=True,
)

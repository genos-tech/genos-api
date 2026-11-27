"""`create_my_schedule` (MIP-1151): MCP-only, see `secretary/__init__.py`."""

from __future__ import annotations

from typing import Any

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.services import schedule_text, schedules
from origin.services.user_time import user_zone
from origin.views.public.mcp.secretary._schedule_args import (
    AI_EFFORT_PROPERTY,
    APP_ONLY,
    PLACEMENT_PROPERTIES,
    RECURRENCE_PROPERTIES,
    guarded,
    job_params_schema,
    placement_args,
    posts_as_owner,
)


def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if posts_as_owner(args.get("job")):
        raise ToolError(APP_ONLY)
    place = placement_args(args)
    sched = guarded(
        lambda: schedules.create(
            ctx,
            name=args.get("name"),
            job=args.get("job"),
            params=place.get("params", args.get("params")),
            deliver_channel_id=place.get("deliver_channel_id"),
            frequency=args.get("frequency"),
            time_of_day=args.get("time_of_day"),
            weekdays=args.get("weekdays"),
            month_day=args.get("month_day"),
            run_date=args.get("run_date"),
            deliver_push=bool(args.get("deliver_push", True)),
            ai_effort=args.get("ai_effort"),
        )
    )
    zone = user_zone(CustomUser.objects.get(pk=ctx.user_id))
    row = schedule_text.present(sched, zone)
    return {"schedule": row, "__summary__": f"Created {row['name']!r}: {row['when']}."}


CREATE_MY_SCHEDULE = Tool(
    name="create_my_schedule",
    description="Create a schedule: run one of Genos's jobs at a time the member chooses.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "A short name the member will recognise."},
            "job": {
                "type": "STRING",
                "description": (
                    "The job's key, from `jobs` in `list_my_schedules`, which says what each "
                    "does, such as what_to_do_today."
                ),
            },
            "params": job_params_schema(),
            **RECURRENCE_PROPERTIES,
            "deliver_push": {
                "type": "BOOLEAN",
                "description": "Also send a push notification with the report. Default true; pass false for a quiet schedule.",
            },
            "ai_effort": AI_EFFORT_PROPERTY,
            **PLACEMENT_PROPERTIES,
        },
        "required": ["name", "job", "frequency", "time_of_day"],
    },
    run=_create,
    requires_approval=True,
    mcp_only=True,
)

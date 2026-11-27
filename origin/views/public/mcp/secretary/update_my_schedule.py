"""`update_my_schedule` (MIP-1151): MCP-only, see `secretary/__init__.py`.

Changes a schedule, and pauses or resumes it (`enabled`): pausing is the undoable way to
stop one, so it is here rather than behind the delete opt-in."""

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
    recurrence_args,
    schedule_row,
    without_message,
)

_CHANGES = ("name", "job", "params", "deliver_push", "ai_effort")


def _update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    schedule_id = args.get("schedule_id")
    changes = {k: args[k] for k in _CHANGES if k in args}
    changes.update(recurrence_args(args))
    enabled = args.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ToolError("`enabled` must be true or false.")
    row = schedule_row(ctx, schedule_id)
    placed = any(k in args for k in PLACEMENT_PROPERTIES)
    # A scheduled message (MIP-2482) can only be paused from here: any other change, or turning it
    # on, would decide what is said in the member's name, where and when.
    if posts_as_owner(args.get("job")) or (
        row is not None and posts_as_owner(row.job) and (changes or placed or enabled is not False)
    ):
        raise ToolError(APP_ONLY)
    if placed:
        changes.update(placement_args(args, row.params if row is not None else None))
    if not changes and enabled is None:
        raise ToolError("Nothing to change: give at least one field, or `enabled`.")
    sched = None
    if changes:
        sched = guarded(lambda: schedules.update(ctx, schedule_id, **changes))
    if enabled is not None:
        sched = guarded(lambda: schedules.set_enabled(ctx, schedule_id, enabled))
    zone = user_zone(CustomUser.objects.get(pk=ctx.user_id))
    row = without_message(schedule_text.present(sched, zone))
    return {"schedule": row, "__summary__": f"Updated {row['name']!r}: {row['when']}."}


UPDATE_MY_SCHEDULE = Tool(
    name="update_my_schedule",
    description="Change, pause or resume one of the member's schedules.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "schedule_id": {"type": "STRING", "description": "From `list_my_schedules`."},
            "name": {"type": "STRING", "description": "A new name."},
            "job": {
                "type": "STRING",
                "description": "A different job's key, from `jobs` in `list_my_schedules`.",
            },
            "params": job_params_schema(),
            **RECURRENCE_PROPERTIES,
            "deliver_push": {"type": "BOOLEAN", "description": "Send a push with the report."},
            "ai_effort": AI_EFFORT_PROPERTY,
            **PLACEMENT_PROPERTIES,
            "enabled": {
                "type": "BOOLEAN",
                "description": "false pauses the schedule, true resumes it from now.",
            },
        },
        "required": ["schedule_id"],
    },
    run=_update,
    requires_approval=True,
    mcp_only=True,
)

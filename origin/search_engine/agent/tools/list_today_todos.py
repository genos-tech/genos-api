"""`list_today_todos` — read-only tool.

Returns the requesting user's todos for their own today, in the team the
call runs in. ACL is implicit: a day's group is per user AND team, so both
are filtered on. Without the team, someone in two teams could be handed the
other team's day, whose ids `update_todo_item` then refuses (MIP-907).
"""

from __future__ import annotations

from typing import Any

from origin.models.chat.todo_models import ToDoGroup
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.todo_args import local_iso
from origin.services import todo_reminders
from origin.services.user_time import today_for_user_id, zone_for_user_id


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    today = today_for_user_id(ctx.user_id)
    group = (
        ToDoGroup.objects.filter(user_id=ctx.user_id, team_id=ctx.team_id, local_date=today)
        .prefetch_related("items", "items__category")
        .first()
    )
    if group is None:
        return {
            "local_date": today.isoformat(),
            "group_id": None,
            "is_completed": False,
            "items": [],
            "__summary__": "No todos for today yet.",
        }

    rows = list(group.items.all().order_by("sort_order", "item_id"))
    # One query for every pending reminder on the day, in the user's zone.
    reminders = todo_reminders.pending_by_item(i.item_id for i in rows)
    zone = zone_for_user_id(ctx.user_id) if reminders else None

    items = []
    completed = 0
    for item in rows:
        if item.is_completed:
            completed += 1
        row = {
            "item_id": item.item_id,
            "title": item.title,
            "is_completed": item.is_completed,
            "category_name": item.category.name if item.category_id else None,
        }
        if item.item_id in reminders:
            row["reminder_at"] = local_iso(reminders[item.item_id], zone)
        items.append(row)

    total = len(items)
    return {
        "local_date": today.isoformat(),
        "group_id": group.group_id,
        "is_completed": group.is_completed,
        "items": items,
        "__summary__": f"Today: {completed} of {total} done.",
    }


LIST_TODAY_TODOS = Tool(
    name="list_today_todos",
    description=(
        "List the user's todo items for today (the current calendar day). "
        "Returns each item with title, completion status, and optional "
        "category, and `reminder_at` when a reminder is pending on it. Use "
        "this when the user asks 'what's on my list today?', "
        "'what's left for today?', or similar. Read-only — does not require "
        "approval."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {},
    },
    run=_run,
    requires_approval=False,
)

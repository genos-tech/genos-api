"""`list_uncompleted_todos` — read-only tool.

Returns the user's still-open todo items across the past N days
(default 14). Capped at 50 items to keep the LLM payload reasonable.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from origin.models.chat.todo_models import ToDoItem
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.todo_args import local_iso
from origin.services import todo_reminders
from origin.services.user_time import today_for_user_id, zone_for_user_id

_MAX_ITEMS = 50
_DEFAULT_DAYS = 14


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        days = int(args.get("date_range_days", _DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = max(1, min(days, 365))

    today = today_for_user_id(ctx.user_id)
    from_date = today - timedelta(days=days)

    qs = (
        ToDoItem.objects.filter(
            group__user_id=ctx.user_id,
            group__local_date__gte=from_date,
            group__local_date__lte=today,
            is_completed=False,
        )
        .select_related("group", "category")
        .order_by("group__local_date", "sort_order", "item_id")[: _MAX_ITEMS + 1]
    )

    fetched = list(qs)
    rows = fetched[:_MAX_ITEMS]
    truncated = len(fetched) > _MAX_ITEMS
    # One query for every pending reminder on the page, in the user's zone.
    reminders = todo_reminders.pending_by_item(i.item_id for i in rows)
    zone = zone_for_user_id(ctx.user_id) if reminders else None

    items = []
    for i in rows:
        row = {
            "item_id": i.item_id,
            "title": i.title,
            "local_date": i.group.local_date.isoformat(),
            "category_name": i.category.name if i.category_id else None,
        }
        if i.item_id in reminders:
            row["reminder_at"] = local_iso(reminders[i.item_id], zone)
        items.append(row)

    summary = f"{len(items)} open item(s) in the last {days} days"
    if truncated:
        summary += f" (showing first {_MAX_ITEMS})"

    return {
        "from_date": from_date.isoformat(),
        "to_date": today.isoformat(),
        "items": items,
        "truncated": truncated,
        "__summary__": summary + ".",
    }


LIST_UNCOMPLETED_TODOS = Tool(
    name="list_uncompleted_todos",
    description=(
        "List the user's still-open todo items across the last N days "
        "(default 14, max 365, capped at 50 items in the response). Use "
        "this when the user asks 'what's still open?', 'what did I miss "
        "this week?', or wants a backlog view of past-but-unfinished "
        "items. Each item carries `reminder_at` when a reminder is "
        "pending on it. Read-only — does not require approval."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "date_range_days": {
                "type": "INTEGER",
                "description": (
                    "How many days back to scan, inclusive of today. Default 14, max 365."
                ),
            },
        },
    },
    run=_run,
    requires_approval=False,
)

"""`list_uncompleted_todos` — read-only tool.

Returns the user's still-open todo items across the past N days
(default 14), in the team the call runs in. Capped at 50 items to keep the
LLM payload reasonable. While `AGENT_TODO_DAYS_AHEAD` is on, `days_ahead` also
returns the open to-dos dated after today (MIP-2456).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings

from origin.models.chat.todo_models import ToDoItem
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.todo_args import local_iso, notes_markdown
from origin.services import todo_reminders
from origin.services.user_time import today_for_user_id, zone_for_user_id

_MAX_ITEMS = 50
_DEFAULT_DAYS = 14

#: MIP-2456 (model-visible, default off): the opt-in look past today.
DAYS_AHEAD_FLAG = "AGENT_TODO_DAYS_AHEAD"
#: Added to the declaration while the flag is on (`tool_flag_params`).
DAYS_AHEAD_PROPERTIES: dict[str, dict[str, Any]] = {
    "days_ahead": {
        "type": "INTEGER",
        "description": (
            "How many days after today to include as well. Default 0 (stops at today), max 365."
        ),
    },
}
#: Added to the description while the flag is on (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " It stops at today: to see to-dos dated tomorrow or later, pass `days_ahead`."
)


def _days_ahead(args: dict[str, Any]) -> int:
    """0 while the flag is off, whatever arrives: the parameter is not declared then."""
    if not settings.SEARCH_ENGINE.get(DAYS_AHEAD_FLAG):
        return 0
    try:
        ahead = int(args.get("days_ahead") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(ahead, 365))


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        days = int(args.get("date_range_days", _DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = max(1, min(days, 365))

    today = today_for_user_id(ctx.user_id)
    from_date = today - timedelta(days=days)
    ahead = _days_ahead(args)
    to_date = today + timedelta(days=ahead)

    qs = (
        ToDoItem.objects.filter(
            group__user_id=ctx.user_id,
            group__team_id=ctx.team_id,
            group__local_date__gte=from_date,
            group__local_date__lte=to_date,
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
        # The notes as markdown (MIP-1334), only when there are some, so an
        # agent reads them before `update_todo_item` replaces them.
        notes = notes_markdown(i)
        if notes:
            row["notes_markdown"] = notes
        if i.item_id in reminders:
            row["reminder_at"] = local_iso(reminders[i.item_id], zone)
        items.append(row)

    summary = f"{len(items)} open item(s) in the last {days} days"
    if ahead:
        summary += f" and the next {ahead}"
    if truncated:
        summary += f" (showing first {_MAX_ITEMS})"

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "items": items,
        "truncated": truncated,
        "__summary__": summary + ".",
    }


LIST_UNCOMPLETED_TODOS = Tool(
    name="list_uncompleted_todos",
    description=(
        "List the user's still-open todo items across the last N days "
        "(default 14, max 365), oldest day first and capped at 50 items: "
        "when `truncated` is true it is the NEWEST items that are left "
        "out, so narrow `date_range_days` to reach them. Use "
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

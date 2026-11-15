"""`create_todo_item` — write tool, requires user approval.

Adds an item to the user's todo group for `target_date` (default today).
Creates the day's group lazily if it doesn't exist yet. `category` is
free-form text — get_or_create on (team, user, name).

`remind_at` / `remind_in_minutes` attach a reminder in the same call —
"remind me Friday to follow up with Sam" is a to-do for Friday plus a
reminder. The time is resolved and checked before anything is written, so a
refused reminder leaves no to-do behind.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from typing import Any

from django.db import transaction

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.search_engine.agent.parity import ignore_unless_parity
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.todo_args import (
    NOTES_TEXT_DESCRIPTION,
    REMIND_AT_SCHEMA,
    REMIND_IN_MINUTES_SCHEMA,
    local_iso,
    notes_blocks,
    reminder_from_args,
)
from origin.search_engine.agent.tools.todo_source import (
    PARITY_PROPERTIES,
    source_block,
    source_from,
)
from origin.services import todo_reminders
from origin.services.user_time import today_for_user_id, zone_for_user_id


def _parse_date(value, default):
    if not value:
        return default
    if isinstance(value, date_cls) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ToolError(f"`target_date` must be ISO date YYYY-MM-DD (got {value!r}).")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = ignore_unless_parity(args, PARITY_PROPERTIES)
    check_body_size(args.get("notes_text"), "notes_text")
    title = (args.get("title") or "").strip()
    if not title:
        raise ToolError("`title` is required.")

    target_date = _parse_date(args.get("target_date"), today_for_user_id(ctx.user_id))
    # Resolved before the first write: a reminder that can't be honoured must
    # not leave a to-do behind that the caller believes was reminded.
    zone = zone_for_user_id(ctx.user_id)
    remind_at = reminder_from_args(args, zone)
    notes_text = (args.get("notes_text") or "").strip()
    category_name = (args.get("category") or "").strip()
    # Checked before the first write, like the reminder: a comment the caller cannot open leaves no
    # to-do behind.
    source = source_from(args, ctx)
    notes = notes_blocks(notes_text, ctx)
    if source is not None:
        notes = [source_block(source), *(notes or [])]

    with transaction.atomic():
        group, _ = ToDoGroup.objects.get_or_create(
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            local_date=target_date,
            defaults={"is_completed": False},
        )
        category = None
        if category_name:
            category, _ = ToDoCategory.objects.get_or_create(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                name=category_name,
            )
        item = ToDoItem.objects.create(
            group=group,
            category=category,
            title=title,
            notes=notes,
        )
        # Newly created incomplete item flips the group's completion
        # back to False if it had been all-done before.
        has_open = ToDoItem.objects.filter(group=group, is_completed=False).exists()
        ToDoGroup.objects.filter(group_id=group.group_id).update(is_completed=not has_open)
        if remind_at is not None:
            todo_reminders.set_reminder(item=item, remind_at=remind_at)

    out: dict[str, Any] = {
        "item_id": item.item_id,
        "group_id": group.group_id,
        "local_date": target_date.isoformat(),
        "title": item.title,
        "category": category.name if category else None,
    }
    summary = f"Created todo for {target_date.isoformat()}: {item.title}"
    if remind_at is not None:
        out["reminder_at"] = local_iso(remind_at, zone)
        out["reminder_time_zone"] = zone.key
        summary += f" (reminder set for {out['reminder_at']})"
    if source is not None:
        out["source"] = {
            "task_id": source.task.task_id,
            "display_id": source.task.display_id,
            "comment_id": source.comment.comment_id if source.comment is not None else None,
        }
        summary += f" ({source.label.lower()} on {source.task.display_id})"
    out["__summary__"] = summary
    return out


CREATE_TODO_ITEM = Tool(
    name="create_todo_item",
    description=(
        "Create a new todo item for the user. REQUIRES USER APPROVAL — the "
        "user will see your proposed arguments and decide whether to "
        "execute. Use this when the user asks to add / create / file a "
        "todo. Required: title. Optional: notes_text (markdown "
        "description), category (free-form label; created if new), "
        "target_date (ISO YYYY-MM-DD; default the user's own today), remind_at / "
        "remind_in_minutes (also remind the user; one at most). 'Remind me "
        "Friday to X' is ONE call: target_date and remind_at both Friday, with "
        "X as the title in the user's words. Call it first: a person named in "
        "X is only text, so there is nobody to look up."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "title": {
                "type": "STRING",
                "description": "Short todo title (1 line).",
            },
            "notes_text": {
                "type": "STRING",
                "description": "Optional body for the todo. " + NOTES_TEXT_DESCRIPTION,
            },
            "category": {
                "type": "STRING",
                "description": (
                    "Optional free-form category label. Existing label "
                    "matched case-sensitively; otherwise created."
                ),
            },
            "target_date": {
                "type": "STRING",
                "description": (
                    "Optional ISO YYYY-MM-DD date. Default is today in the user's own time zone."
                ),
            },
            "remind_at": REMIND_AT_SCHEMA,
            "remind_in_minutes": REMIND_IN_MINUTES_SCHEMA,
        },
        "required": ["title"],
    },
    run=_run,
    requires_approval=True,
)

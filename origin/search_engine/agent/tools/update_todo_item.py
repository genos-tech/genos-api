"""`update_todo_item` — write tool, requires user approval.

Patch fields on an existing todo item the user owns. Common use:
toggle `is_completed`, edit `title`, change `category`, set / clear
`notes_text`, move it to another day (`target_date`), and set or cancel
its reminder (`remind_at`, `remind_in_minutes`, `cancel_reminder`).

The day move, completion and reminder rules are the UI's own
(`services/todo_items`, `services/todo_reminders`): sub-items follow their
parent to the new day, and ticking a to-do off retires its pending reminder.

`category` is a NAME, found or created for the user, where the REST endpoint
takes a `category_id`: an agent has names and no tool lists ids. The rules
around it are the endpoint's (`services/todo_items`): a sub-item's category is
its parent's, so it can't be set on one, and setting it on a parent carries it
to its sub-items.

ACL: the requesting user must own the group the item belongs to.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from origin.models.chat.todo_models import ToDoCategory
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.todo_args import (
    NOTES_TEXT_DESCRIPTION,
    REMIND_AT_SCHEMA,
    REMIND_IN_MINUTES_SCHEMA,
    load_owned_item,
    local_iso,
    notes_blocks,
    parse_iso_date,
    reminder_from_args,
)
from origin.services import todo_items, todo_reminders
from origin.services.user_time import zone_for_user_id


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_body_size(args.get("notes_text"), "notes_text")
    item = load_owned_item(args.get("item_id"), ctx)
    zone = zone_for_user_id(ctx.user_id)

    # Everything that can be refused is checked here, before the first write:
    # a refused call changes nothing, even when it also carried valid edits.
    new_title = None
    if "title" in args and args["title"] is not None:
        new_title = (args["title"] or "").strip()
        if not new_title:
            raise ToolError("`title`, if provided, must not be blank.")

    move_to = None
    if args.get("target_date") not in (None, ""):
        move_to = parse_iso_date(args["target_date"], "target_date")
        try:
            todo_items.check_can_move(item)
        except todo_items.TodoItemError as exc:
            raise ToolError(str(exc)) from None

    category_name = (args.get("category") or "").strip()
    category_change = bool(args.get("clear_category")) or bool(category_name)
    if category_change:
        # A sub-item's category is its parent's, as in the app.
        try:
            todo_items.check_can_set_category(item)
        except todo_items.TodoItemError as exc:
            raise ToolError(str(exc)) from None

    cancel_reminder = bool(args.get("cancel_reminder"))
    remind_at = reminder_from_args(args, zone)
    if cancel_reminder and remind_at is not None:
        raise ToolError(
            "`cancel_reminder` can't be combined with `remind_at` or `remind_in_minutes`: "
            "to change the reminder, just set the new one."
        )
    if remind_at is not None:
        # Judged on the state the call leaves behind, the one the REST
        # endpoint sees on a separate request: reopening and re-reminding in
        # one call is fine; ticking off and reminding is a contradiction.
        completing = args.get("is_completed")
        if completing:
            raise ToolError("A to-do this call marks complete takes no reminder.")
        if completing is None and item.is_completed:
            raise ToolError(
                f"{todo_reminders.ALREADY_COMPLETED} Reopen it (`is_completed: false`) "
                "to give it a reminder."
            )

    with transaction.atomic():
        if new_title is not None:
            item.title = new_title

        # Notes — replace with the markdown parsed to BlockNote (MIP-1334).
        # Pass an empty string to clear notes entirely.
        if "notes_text" in args:
            item.notes = notes_blocks((args.get("notes_text") or "").strip(), ctx)

        # Completion — ticking it off retires the pending reminder.
        if "is_completed" in args and args["is_completed"] is not None:
            todo_items.apply_completion(item, bool(args["is_completed"]))

        # Category (free-form). `clear_category=true` removes it
        # explicitly; passing a non-empty `category` overrides any clear.
        if args.get("clear_category"):
            item.category = None
        if category_name:
            category, _ = ToDoCategory.objects.get_or_create(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                name=category_name,
            )
            item.category = category

        # Saves the row, re-homing it into the target day (children follow)
        # and recomputing the completion of every day it touched.
        saved = todo_items.save_item(item, move_to=move_to)
        if category_change:
            todo_items.cascade_category(item)

        if cancel_reminder:
            todo_reminders.cancel_pending(item=item)
        if remind_at is not None:
            todo_reminders.set_reminder(item=item, remind_at=remind_at)
    saved.purge_stale_chunks()

    item.refresh_from_db()
    reminder = todo_reminders.pending_by_item([item.item_id]).get(item.item_id)
    out: dict[str, Any] = {
        "item_id": item.item_id,
        "group_id": item.group_id,
        "local_date": item.group.local_date.isoformat(),
        "title": item.title,
        "is_completed": item.is_completed,
        "category": item.category.name if item.category_id else None,
        "reminder_at": local_iso(reminder, zone),
    }
    notes = []
    if saved.moved:
        notes.append(f"moved to {out['local_date']}")
    if remind_at is not None:
        out["reminder_time_zone"] = zone.key
        notes.append(f"reminder set for {out['reminder_at']}")
    elif cancel_reminder:
        notes.append("reminder cancelled")
    suffix = f" ({', '.join(notes)})" if notes else ""
    out["__summary__"] = f"Updated todo {item.item_id}: {item.title}{suffix}"
    return out


UPDATE_TODO_ITEM = Tool(
    name="update_todo_item",
    description=(
        "Update fields on one of the user's existing todo items. REQUIRES "
        "USER APPROVAL. Common uses: mark complete/incomplete, edit title, "
        "change category, replace or clear the notes body, move it to "
        "another day, set or cancel its reminder. Required: item_id. "
        "Optional patch fields: title, notes_text (markdown; replaces the "
        "whole body, empty string clears it), is_completed, category (free-form), clear_category "
        "(set true to remove the existing category; a sub-item takes its "
        "parent's category, so set it on the parent), target_date (move to "
        "that day), remind_at / remind_in_minutes (remind the user; one at "
        "most), cancel_reminder. Completing a to-do cancels its reminder."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "item_id": {
                "type": "INTEGER",
                "description": "ID of the todo item to update.",
            },
            "title": {
                "type": "STRING",
                "description": "New title. Must be non-blank if provided.",
            },
            "notes_text": {
                "type": "STRING",
                "description": (
                    "Replace the item's notes body with this. Pass an empty "
                    "string to clear notes. Read the current notes first "
                    "(`notes_markdown` from list_today_todos / "
                    "list_uncompleted_todos) and send them back with your "
                    "change, so formatting the user typed is kept. "
                )
                + NOTES_TEXT_DESCRIPTION,
            },
            "is_completed": {
                "type": "BOOLEAN",
                "description": "Mark the item complete (true) or reopen it (false).",
            },
            "category": {
                "type": "STRING",
                "description": (
                    "Free-form category label. Existing matched "
                    "case-sensitively; otherwise created."
                ),
            },
            "clear_category": {
                "type": "BOOLEAN",
                "description": (
                    "Set to true to remove the existing category. Ignored "
                    "if `category` is also provided."
                ),
            },
            "target_date": {
                "type": "STRING",
                "description": (
                    "Move the to-do to this day (ISO YYYY-MM-DD), taking its "
                    "sub-items along. A sub-item can't be moved on its own: "
                    "name its parent."
                ),
            },
            "remind_at": REMIND_AT_SCHEMA,
            "remind_in_minutes": REMIND_IN_MINUTES_SCHEMA,
            "cancel_reminder": {
                "type": "BOOLEAN",
                "description": (
                    "Set to true to cancel the to-do's pending reminder. Not "
                    "with `remind_at` / `remind_in_minutes`."
                ),
            },
        },
        "required": ["item_id"],
    },
    run=_run,
    requires_approval=True,
)

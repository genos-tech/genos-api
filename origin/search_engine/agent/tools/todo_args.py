"""What the to-do write tools share: finding the to-do, and reminder arguments.

`update_todo_item` and `delete_todo_item` both act on one existing to-do, and
the rule for "is this the caller's" has to be the same word for word, so it is
spelled once (`load_owned_item`). `create_todo_item` and `update_todo_item`
both take a reminder, and the resolution of "when" is the server's job
(`services/reminder_input`), so the arguments and their text are spelled once
too.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from origin.models.chat.todo_models import ToDoItem
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import todo_notes_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.services.reminder_input import (
    DEFAULT_REMINDER_HOUR,
    ReminderInputError,
    resolve_remind_at,
)

# The schema of the two ways to ask for a reminder. The text is the model's
# whole instruction for them, so it is written once and both tools spread it.
REMIND_AT_SCHEMA = {
    "type": "STRING",
    "description": (
        "Optional: remind the user about this to-do at a clock time. A local "
        "date-time like 2026-11-06T09:00 is read in the user's own time zone; "
        f"a bare date like 2026-11-06 means {DEFAULT_REMINDER_HOUR}:00 that "
        "day. Must be in the future and within a year. If you do not know "
        "the current time, use `remind_in_minutes` for 'in an hour'. One "
        "reminder per to-do: a new one replaces the old. Not with "
        "`remind_in_minutes`."
    ),
}
REMIND_IN_MINUTES_SCHEMA = {
    "type": "INTEGER",
    "description": (
        "Optional: remind the user this many minutes from now, counted from "
        "the moment the call actually runs, not from when it was proposed. "
        "At least 1, at most a year. Not with `remind_at`."
    ),
}


# What `notes_text` is, for both write tools (MIP-1334). The notes editor is
# BlockNote, so a note takes the same markdown as a task body; a `@` shows the
# person and pings nobody, since to-dos notify no one.
NOTES_TEXT_DESCRIPTION = (
    "Markdown: links `[text](https://...)`, **bold**, *italic*, `code`, "
    "lists, `- [ ]` checklists, code fences and tables render as rich text. "
    "Each line stays its own line. A task's display id written bare "
    "(`#PRJ-123`) or `[#title](task:<id>)` shows as a chip, and "
    "`[@name](user:<uuid>)` shows the person, but a to-do notifies no one."
)


def notes_blocks(text: str, ctx: ToolContext) -> list[dict[str, Any]] | None:
    """`notes_text` as the BlockNote document `ToDoItem.notes` stores, or None
    for empty text (no notes). Chips resolve against what the caller can read."""
    return todo_notes_blocks(
        text, entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id)
    )


def notes_markdown(item: ToDoItem) -> str | None:
    """A to-do's notes as markdown for a read tool, None when it has none. The
    inverse of `notes_blocks`: what an agent reads back it can write back, so an
    edit keeps the links and lists a person typed in the editor."""
    if not item.notes:
        return None
    return blocks_to_markdown(item.notes).strip() or None


def load_owned_item(raw_id: Any, ctx: ToolContext) -> ToDoItem:
    """The to-do `raw_id` names, when it is on the caller's own list in the
    caller's team. A to-do has no `user` column — ownership is its group's —
    so every per-item tool makes this walk, as `_get_owned_item` does for the
    REST views.

    A to-do that is someone else's, or in another team, gets the very answer
    a missing one gets, word for word, so a caller can't probe which ids
    exist. It is still a `ToolForbidden` rather than a plain `ToolError`: the
    text is identical, but the MCP refusal-burst alert counts only the
    former, and probing other people's ids is what it is there to see."""
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f"`item_id` must be an integer (got {raw_id!r}).")

    not_found = f"Todo item {item_id} not found."
    try:
        item = ToDoItem.objects.select_related("group").get(item_id=item_id)
    except ToDoItem.DoesNotExist:
        raise ToolError(not_found)

    if str(item.group.user_id) != ctx.user_id:
        raise ToolForbidden(not_found)
    if str(item.group.team_id or "") != ctx.team_id:
        raise ToolForbidden(not_found)
    return item


def parse_iso_date(value: Any, name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(f"`{name}` must be ISO date YYYY-MM-DD (got {value!r}).")


def reminder_from_args(args: dict[str, Any], zone: ZoneInfo) -> datetime | None:
    """The instant `remind_at` / `remind_in_minutes` ask for, or None when
    neither was given. Every refusal is a `ToolError` the model can act on."""
    try:
        return resolve_remind_at(
            remind_at=args.get("remind_at"),
            remind_in_minutes=args.get("remind_in_minutes"),
            zone=zone,
        )
    except ReminderInputError as exc:
        raise ToolError(str(exc)) from None


def local_iso(instant: datetime | None, zone: ZoneInfo) -> str | None:
    """`instant` as the user would read it: their zone, with the offset. A
    bare UTC value would have the model tell a Tokyo user "midnight"."""
    if instant is None:
        return None
    return instant.astimezone(zone).isoformat(timespec="seconds")

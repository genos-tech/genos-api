"""`delete_todo_item` — write tool, requires user approval.

Delete one of the user's own to-dos. DESTRUCTIVE: the REST delete this
mirrors is a hard delete that cascades to the to-do's sub-items and to every
reminder on them, and nothing keeps a copy. Both go through the same
implementation (`services/todo_items.delete_item`), so the search index is
purged and the day's completion flag recomputed the way the UI's delete does.

Ticking a to-do off (`update_todo_item` with `is_completed`) keeps it on the
list and is the right call for "I finished that"; this is for "remove it".

ACL: the requesting user must own the group the item belongs to.
"""

from __future__ import annotations

from typing import Any

from origin.models.chat.todo_models import ToDoItem
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.todo_args import load_owned_item
from origin.services import todo_items


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    item = load_owned_item(args.get("item_id"), ctx, action="delete")

    item_id, title, local_date = item.item_id, item.title, item.group.local_date.isoformat()
    sub_items = ToDoItem.objects.filter(parent_item_id=item_id).count()

    todo_items.delete_item(item)

    summary = f"Deleted todo {item_id}: {title}"
    if sub_items:
        summary += f" (and {sub_items} sub-item{'s' if sub_items != 1 else ''})"
    # No `local_date` beside `item_id`: that pair is what makes a citable
    # link, and there is nothing left to link to.
    return {
        "item_id": item_id,
        "title": title,
        "deleted": True,
        "deleted_from_date": local_date,
        "deleted_sub_items": sub_items,
        "__summary__": summary,
    }


DELETE_TODO_ITEM = Tool(
    name="delete_todo_item",
    description=(
        "Permanently delete one of the user's todo items, with its "
        "sub-items and reminders. REQUIRES USER APPROVAL — destructive and "
        "not undoable. Use this only when the user explicitly asks to "
        "delete / remove a todo; to record that it is done, use "
        "update_todo_item with is_completed instead. Required: item_id. "
        "The todo no longer exists afterwards, so there is nothing to "
        "link: name it in plain text."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "item_id": {
                "type": "INTEGER",
                "description": "ID of the todo item to delete.",
            },
        },
        "required": ["item_id"],
    },
    run=_run,
    requires_approval=True,
)

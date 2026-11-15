"""A refusal sentence for a text field that is too long for its column (MIP-1367, phase 1).

A title that does not fit its `varchar` used to fail deep inside the write, and the caller read an
opaque "Failed to create task (ref ...)" (the real cause, a `DataError`, went to the log), or, for a
to-do, "this is a bug in Genos". A model or a person can act on neither. These checks run on the
value AFTER the tool's own normalisation (the `.strip()` each one already does), before anything is
written, with the limit read from the model's own field so it can never drift from the column.

They refuse exactly what the database would have refused, so a call that worked still works, byte
for byte; only the words of a failure change. Nothing here touches a tool's declaration (the schema
a client reads).

`check_text_length` raises the `ToolError` a tool already raises for a bad argument, so MCP shows it
as an `isError` result the model can retry against, and the in-app agent as a tool error.
"""

from __future__ import annotations

import os
from typing import Any

from origin.models.chat.todo_models import ToDoCategory, ToDoItem
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolError


def column_limit(model, field: str) -> int:
    return model._meta.get_field(field).max_length


TASK_TITLE_MAX = column_limit(TaskMaster, "title")
MILESTONE_TITLE_MAX = column_limit(MilestoneMaster, "title")
# One limit for the three note families: they all share it today, and the smallest is safe.
NOTE_TITLE_MAX = min(
    column_limit(PersonalNoteMaster, "title"),
    column_limit(TaskNoteMaster, "title"),
    column_limit(ChatNoteMaster, "title"),
)
TODO_TITLE_MAX = column_limit(ToDoItem, "title")
TODO_CATEGORY_MAX = column_limit(ToDoCategory, "name")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


# `search_workspace` embeds its query, and a query of 2,000,000 characters reached the embedding
# (MIP-1367 audit). A question a person or a model really asks is a sentence or two. MCP only:
# the in-app agent's own calls are unchanged.
MCP_SEARCH_QUERY_MAX_CHARS = _env_int("MCP_SEARCH_QUERY_MAX_CHARS", 1000)


def too_long_message(
    value_len: int, field: str, limit: int, *, nothing: str, body: str | None
) -> str:
    advice = (
        f"Keep it to one short line and put the detail in `{body}`, then send the call again."
        if body
        else "Shorten it and send the call again."
    )
    return (
        f"`{field}` is too long: {value_len} characters were sent and the limit is {limit}. "
        f"{nothing} {advice}"
    )


def check_text_length(
    value: Any,
    field: str,
    limit: int,
    *,
    nothing: str = "Nothing was changed.",
    body: str | None = None,
) -> None:
    """Raise a `ToolError` that names the field, the limit and what was left alone when `value`
    (a string, already stripped by the caller) has more than `limit` characters. Anything that is
    not a string is the tool's own argument handling's to judge."""
    if isinstance(value, str) and len(value) > limit:
        raise ToolError(too_long_message(len(value), field, limit, nothing=nothing, body=body))

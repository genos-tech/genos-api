"""Fence user-authored names for the model, at the boundary (MIP-735).

A task title is written by whoever can create a task, and so is a project
name. An instruction put in one reaches the model as ordinary tool output,
and the prompt only tells it to treat text inside `<workspace_content>` as
data. The read tools already fence bodies, comments and snippets; the short
strings next to them (`title`, `project_name`) came back bare.

**Why here and not in each tool.** Two readers need those strings bare:

  * the controller builds the source chips under every answer by reading
    `title` / `project_name` out of the tool result, and a fence would show
    up in the chip;
  * the model passes some names straight back to a write tool.

So the tools keep returning bare values, and the copy that goes to the
MODEL is fenced as it is turned into a `function_response`. The chips, the
persisted step JSON, the eval harness and the tools themselves never see a
fence, and a tool added tomorrow is covered without remembering to wrap.

**What is fenced is a reviewed list**, by key name, at any depth: the keys in
`FENCED_KEYS` everywhere, and the keys in `FENCED_KEYS_BY_TOOL` only in the
named tool's result. It is deliberately NOT "every string":

  * ids, enums, dates, urls the tools build, and `error` are not user text;
  * a TAG NAME is an identifier the model passes back verbatim to
    `create_task` / `update_task` (`tags=[...]`), and a fence would be echoed
    back as part of the name. `list_project_tags` says so itself. Tag names
    are also capped at 20 characters. They are in `EXEMPT_KEYS` /
    `EXEMPT_TOOL_KEYS` with that reason, and a test holds the sets apart.

**Why some keys are per tool (MIP-801).** `name` is a sprint's or a note
folder's in three tools (display text) and a TAG in `list_project_tags` (an
identifier). The same key means two things, so it is fenced by (tool, key),
never by key alone.

**Keys the boundary cannot fence (MIP-867).** `get_task_history`'s `old_value`
and `new_value` hold a task title, a username or a file name in one entry and
an id, an enum or a date in the next, so no key rule is right for both. The
tool fences by the kind of change it is reporting, and `FENCED_IN_TOOL` records
that, so the key is a decision on the page and not a gap.

Idempotent: a value a tool already fenced is left as it is, and the persisted
steps replayed on a resume go through the same function.
"""

from __future__ import annotations

from typing import Any

# Short strings a person typed, which the model reads out to the user and
# never passes back as an identifier.
FENCED_KEYS = frozenset(
    {
        "title",
        "project_name",
    }
)

# Fenced only in the results of the tools named here (MIP-801): the key is
# display text in these tools but means something else elsewhere, or the
# tool is the only place a given key holds user text.
#
# Nothing here is passed back to a tool as an identifier: the write tools take
# ids (`assign_task` a user id, the folder and sprint tools an id, the calendar
# tools an event or calendar id). `unfence_arguments` would also strip a fence
# the model copied into an argument.
_PEOPLE = frozenset({"username"})
_ATTENDEE_NAMES = frozenset({"displayName"})
FENCED_KEYS_BY_TOOL: dict[str, frozenset[str]] = {
    # A sprint's or a folder's name.
    "get_sprint_summary": frozenset({"name"}),
    "list_sprints": frozenset({"name"}),
    "list_note_folders": frozenset({"name"}),
    # A person's display name.
    "get_team_members": _PEOPLE,
    "list_project_members": _PEOPLE,
    "list_channel_members": _PEOPLE,
    "get_workload_distribution": _PEOPLE,
    "get_top_task_closers": _PEOPLE,
    "get_milestone_assignee_counts": _PEOPLE,
    "get_milestone_summary": _PEOPLE,
    "get_current_user": _PEOPLE,
    # Not in MIP-801's list: `actor.username` on every history entry. And
    # `via.key_name` (MIP-870): whoever creates an API key names it, and a write
    # through that key stamps the name on the entry. Nothing takes it back as an
    # argument, so it is display text. `key_id` and `key_prefix` are generated.
    "get_task_history": _PEOPLE | {"key_name"},
    # A calendar's name. An EVENT's `summary` (list/create/update_calendar_event)
    # is already fenced by `calendar.shape_event`; it is listed for
    # `create_calendar_event` so the fence holds if that changes. Idempotent.
    "list_calendars": frozenset({"summary"}),
    "create_calendar_event": frozenset({"summary", "displayName"}),
    # A calendar attendee's name (MIP-867), `attendees[].displayName` out of
    # `calendar.shape_event`. Google lets whoever creates an event write the
    # name of each attendee, so it is not the invitee's own word. The model
    # addresses attendees by `email`, which stays bare: `attendees` in the
    # create/update tools is a list of emails and nothing takes a name back.
    # `get_my_schedule` is the fourth caller of `shape_event`.
    "list_calendar_events": _ATTENDEE_NAMES,
    "update_calendar_event": _ATTENDEE_NAMES,
    "get_my_schedule": _ATTENDEE_NAMES,
}

# User-typed, but the model passes them back to a write tool verbatim, so
# fencing would break the call. Listed so the exemption is a decision on the
# page and not an accident of what is missing from the fenced sets, and so a
# test can refuse a key in both.
EXEMPT_KEYS = {
    "tags": "a task's tag names: identifiers the model passes back verbatim as `tags=[...]`",
}

# (tool, key) exemptions: the key is fenced in other tools, but here it holds
# an identifier.
EXEMPT_TOOL_KEYS = {
    ("list_project_tags", "name"): (
        "a tag name: an identifier the model passes back verbatim as `tags=[...]`"
    ),
}

# (tool, key) pairs the boundary must NOT fence by key, because the same key
# holds user text AND identifiers and only the tool knows which (MIP-867). The
# tool fences the user-typed values itself, by what kind of change the entry
# records, and leaves the rest bare. A key rule here would wrap the ids, enums
# and dates the model passes back to a write tool.
FENCED_IN_TOOL = {
    ("get_task_history", "old_value"): (
        "`get_task_history._value` fences it by kind of change: a title, a username, a "
        "file name, a custom-field value, a tag, a relation's label and any status, "
        "priority or effort outside the product's own set are wrapped; ids, display ids, "
        "dates, booleans and the known enum values stay bare for the model to pass back"
    ),
    ("get_task_history", "new_value"): "the same entries as `old_value`, the other side",
}

# User-typed strings the tools return that nothing fences yet. Each needs a
# decision of its own (does the model pass it back, and is the key shared with
# a non-text field), not a blanket rule. A string found bare goes here until it
# has that decision, and `FENCED_KEYS_BY_TOOL`, `EXEMPT_*` or `FENCED_IN_TOOL`
# takes it when it does.
UNFENCED_FOLLOW_UP: dict[str, str] = {}

_OPEN = "<workspace_content>"


def _fence(value: str) -> str:
    # A tool that already fenced it (fetch_pr, search_kb, ...) is left alone,
    # and so is a stored result replayed on a resume.
    if not value or value.lstrip().startswith(_OPEN):
        return value
    # Imported here, not at the top: `tools/__init__.py` imports THIS module
    # to wrap every tool, and importing `tools.base` runs that `__init__`
    # first, so a top-level import is a cycle that only shows when this
    # module happens to be imported before the package.
    from origin.search_engine.agent.tools.base import wrap_workspace_content  # noqa: PLC0415

    return wrap_workspace_content(value)


def fence_for_model(value: Any, tool_name: str | None = None) -> Any:
    """`value` with every fenced string fenced, at any depth: the
    `FENCED_KEYS`, plus the `FENCED_KEYS_BY_TOOL` keys of `tool_name`'s result.

    Returns a new structure; the argument is not modified, because the same
    dict is also what the chips and the persisted step are built from.
    """
    return _fence_keys(value, FENCED_KEYS | FENCED_KEYS_BY_TOOL.get(tool_name or "", frozenset()))


def _fence_keys(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _fence(item) if key in keys and isinstance(item, str) else _fence_keys(item, keys)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_fence_keys(item, keys) for item in value]
    return value


_CLOSE = "</workspace_content>"


def _unfence(text: str) -> str:
    """The text inside a `<workspace_content>` fence, if `text` is exactly one.

    Only a value that STARTS with the opening tag and ENDS with the closing
    one is unwrapped, so a title that merely mentions the tag, or a body that
    holds a fence somewhere in the middle, is not touched.
    """
    stripped = text.strip()
    if not (stripped.startswith(_OPEN) and stripped.endswith(_CLOSE)):
        return text
    inner = stripped[len(_OPEN) : -len(_CLOSE)]
    # `wrap_workspace_content` puts a newline inside each side.
    if inner.startswith("\n"):
        inner = inner[1:]
    if inner.endswith("\n"):
        inner = inner[:-1]
    # One fence only: an inner tag means this was not the wrapper's output.
    if _OPEN in inner or _CLOSE in inner:
        return text
    return inner


def unfence_arguments(value: Any) -> Any:
    """`value` (a tool's arguments) with any fence the model copied from a
    result taken back off, at any depth.

    The model sees fenced titles and bodies, and a tool that asks it to send
    a title or body "verbatim" can get the fence back as part of the text.
    Stripping it here, once, covers every tool including one added later, and
    the nested specs of `create_task_plan`.
    """
    if isinstance(value, str):
        return _unfence(value)
    if isinstance(value, dict):
        return {key: unfence_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unfence_arguments(item) for item in value]
    return value


def with_unfenced_arguments(run):
    """`run` for a Tool, taking a copied fence off its arguments first."""

    def wrapped(args, ctx):
        return run(unfence_arguments(args), ctx)

    wrapped.__wrapped__ = run
    return wrapped

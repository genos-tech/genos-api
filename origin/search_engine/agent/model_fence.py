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

**What is fenced is a reviewed list** (`FENCED_KEYS`), by key name, at any
depth. It is deliberately NOT "every string":

  * ids, enums, dates, urls the tools build, and `error` are not user text;
  * a TAG NAME is an identifier the model passes back verbatim to
    `create_task` / `update_task` (`tags=[...]`), and a fence would be echoed
    back as part of the name. `list_project_tags` says so itself. Tag names
    are also capped at 20 characters. They are in `EXEMPT_KEYS` with that
    reason, and a test holds the two sets apart.

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

# User-typed, but the model passes them back to a write tool verbatim, so
# fencing would break the call. Listed so the exemption is a decision on the
# page and not an accident of what is missing from FENCED_KEYS, and so a test
# can refuse a key in both sets.
#
# `tags` is the only key here on purpose. `name` is NOT exempt as a key: in
# `list_project_tags` it is a tag (an identifier), but `get_sprint_summary`,
# `list_sprints` and `list_note_folders` also return `name` for a sprint or a
# folder, which are display text. Until the key is split by tool it is simply
# not fenced, which is a gap (see UNFENCED_FOLLOW_UP), not an exemption.
EXEMPT_KEYS = {
    "tags": "a task's tag names: identifiers the model passes back verbatim as `tags=[...]`",
}

# User-typed strings the tools return that this module does NOT fence yet,
# found by reading every tool (MIP-735). Each needs a decision of its own
# (per-tool key, or whether the model passes it back), not a blanket rule.
UNFENCED_FOLLOW_UP = {
    "name": "sprint names and note-folder names are display text; tag names are not (see above)",
    "username": "a person's display name; eight tools return it",
    "summary": "a calendar event's title (list_calendars / create_calendar_event)",
}

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


def fence_for_model(value: Any) -> Any:
    """`value` with every `FENCED_KEYS` string fenced, at any depth.

    Returns a new structure; the argument is not modified, because the same
    dict is also what the chips and the persisted step are built from.
    """
    if isinstance(value, dict):
        return {
            key: (
                _fence(item)
                if key in FENCED_KEYS and isinstance(item, str)
                else fence_for_model(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [fence_for_model(item) for item in value]
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

"""Edit before approve: what a person may change on an approval card, and how
the change reaches the tool.

The agent proposes a write, the card shows it, and the person can fix a title, a
date or a sentence before approving instead of rejecting and asking again.
`POST /api/v2/agent/decide/` takes an optional `edited_args` on an APPROVE: a
flat `{path: new value}` patch, with paths like `title`, `content_text` or, for
a bulk tool, a row path (`updates[3].priority`). The client never holds the raw
arguments (the card shows `_friendly_arguments`, with ids turned into labels), so
an edit is a patch merged here, server-side, onto the persisted `arguments_json`.

**One map, one place** (`EDITABLE_FIELDS`, in the style of `tools/categories.py`).
Every approval-gated tool is a key; a tool with nothing editable says so with an
empty map, so a new write tool cannot be added without deciding, and
`test_agent_approval_edits` enforces that in both directions. Nothing outside
the map is editable, and v1 is plain values only: text, a choice from a fixed
list, a date, a reminder time, tag names. No target id (`task_id`, `note_id`,
`project_id`, `assignee_id`, `milestone_id`, `folder_id`), nothing that needs a
picker, no delete / approve / decline / restore / sharing / members tool, no
`create_task_plan` rows, no calendar tool. An editable path must also be PRESENT in
this call (`editable_paths`): an edit changes a value the model proposed, it does
not add a field, so `content_text` and `body_append` (or `remind_at` and
`remind_in_minutes`) can never end up in one call.

**Validated before the approval is spent.** `prepare` runs between the checks on
the run and the atomic consume of the one-shot approval token, so a typo is
a 422 on a card that still works, not a burnt approval and a refused write. The
rules are the ones the tool applies in its own `_run`, restated here in words for
a person (the tools' `ToolError` texts are written for the model and are never
passed on). Where a tool's check is looser than a person's rule (a date `date.fromisoformat`
accepts as `20261120`), the edit is held to the stricter one: an edit must never be
accepted here and refused by the tool. `test_agent_approval_edits` pins that
against the real tools, field by field. What is left to the tool at run time is
what only the database knows: that a tag exists, that the caller may edit the task.

**An edit cannot widen access.** The tool that runs is the same `tool.run`, on the
same context, with the merged arguments: the same permission checks, the same
ACL, the same fences. No reference-typed field is editable, so `resolve_call` is
not re-run (a test holds the allow-list apart from every reference argument).

**What the model sees does not change.** `arguments_json` stays the model's own
call (it is replayed to the model as its `function_call`, with its
`thought_signature`); the person's patch is stored beside it as
`AgentStep.edited_arguments_json`. KNOWN LIMITATION: after an edit the model's
replay still shows its original call next to the result of what ran, so its final
answer can describe the proposal rather than the edit. Telling the model what was
edited changes what it sees, which needs an eval; it is a follow-up.

**Text a person typed is not a copy of a fence.** Every tool takes its arguments
through `with_unfenced_arguments`, which strips a fence the MODEL copied from a
result and undoes the fence escape. Applied to typed text that would change it, so
`apply_edits(for_run=True)` escapes edited strings first (`escape_user_arguments`),
and the tool receives exactly what was typed. The other half: a PROPOSAL can carry a
fence the model copied (the read tools fence the bodies it is told to send back), which
the tool strips. So the card shows an editable value as the tool would receive it
(`for_card`), or a person who fixed one word of a note would save the tags as text.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from origin.models.chat.todo_models import ToDoItem
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.model_fence import escape_user_arguments, unfence_arguments
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import check_body_size, max_body_bytes
from origin.services.reminder_input import ReminderInputError, resolve_remind_at
from origin.services.user_time import zone_for_user_id

# --------------------------------------------------------------------------- #
# The allow-list                                                              #
# --------------------------------------------------------------------------- #

TEXT = "text"  # a short line: required or not, capped by the column
BODY = "body"  # a long text: capped by `AGENT_MAX_BODY_BYTES`
ENUM = "enum"  # one of the tool's own enum, as its schema declares it
DATE = "date"  # YYYY-MM-DD
REMIND_AT = "remind_at"  # a local date-time in the user's zone, in the future
TAGS = "tags"  # a list of tag names; whether they exist is the tool's to say


@dataclass(frozen=True)
class Field:
    """One editable path of one tool, and the rule its new value must meet."""

    kind: str
    # How the card and the error messages name it.
    label: str
    # TEXT / BODY: a value that is blank once stripped is refused. Only where
    # the tool refuses it too (a title, a comment, an appended passage).
    required: bool = False
    # TEXT: the most characters the column holds, after the tool's own strip.
    max_length: int | None = None
    # DATE / REMIND_AT: `''` is accepted. Only where the tool reads `''` as
    # "none" or "clear": where it reads `''` as "no change" (a to-do's move and
    # reminder on update) the card would show a clear that never happens.
    allow_empty: bool = False
    # ENUM: values the tool accepts and the editor must not offer.
    exclude: tuple[str, ...] = ()


def _title(max_length: int) -> Field:
    return Field(TEXT, "Title", required=True, max_length=max_length)


# The column limits, read off the models: a longer title is a database error
# the tools turn into "Failed to ..." AFTER the approval has been spent.
_TASK_TITLE = TaskMaster._meta.get_field("title").max_length
_NOTE_TITLE = min(
    model._meta.get_field("title").max_length
    for model in (PersonalNoteMaster, TaskNoteMaster, ChatNoteMaster)
)
_TODO_TITLE = ToDoItem._meta.get_field("title").max_length

_PRIORITY = Field(ENUM, "Priority")
_EFFORT = Field(ENUM, "Effort")
# `update_task` also takes "Deleted"; an edit does not offer it. A delete is its
# own decision, and for a milestone's backing task the tool refuses it anyway,
# at run time, after the approval is gone.
_STATUS = Field(ENUM, "Status", exclude=("Deleted",))

_NONE: dict[str, Field] = {}

# tool name -> {path: Field}. A path is a top-level argument (`title`) or, for a
# bulk tool, a field of every row (`updates[*].priority`; a request names one row,
# `updates[3].priority`). A tool with nothing editable is an explicit `{}`.
EDITABLE_FIELDS: dict[str, dict[str, Field]] = {
    # --- tasks ---
    "create_task": {
        "title": _title(_TASK_TITLE),
        "content_text": Field(BODY, "Description"),
        "priority": _PRIORITY,
        "effort_level": _EFFORT,
        # `''` is "no date" here: the tool skips an empty one.
        "due_date": Field(DATE, "Due date", allow_empty=True),
        "start_date": Field(DATE, "Start date", allow_empty=True),
    },
    "update_task": {
        "title": _title(_TASK_TITLE),
        # `''` clears the body. The other two add to it and need something to add.
        "content_text": Field(BODY, "Description"),
        "body_append": Field(BODY, "Text to add", required=True),
        "body_prepend": Field(BODY, "Text to add", required=True),
        "status": _STATUS,
        "priority": _PRIORITY,
        "effort_level": _EFFORT,
        "due_date": Field(DATE, "Due date", allow_empty=True),
        "start_date": Field(DATE, "Start date", allow_empty=True),
        "tags": Field(TAGS, "Tags"),
    },
    # Rows of a bulk update: the scalars only. Not `task_id`, `milestone_id`,
    # `assignee_id`, `tags` (a name must exist in each task's project) or the
    # `rationale`, which is the model's sentence to the person reading the card.
    "update_tasks_bulk": {
        # The bulk tool's own enum already leaves "Deleted" out.
        "updates[*].status": Field(ENUM, "Status"),
        "updates[*].priority": _PRIORITY,
        "updates[*].effort_level": _EFFORT,
        "updates[*].due_date": Field(DATE, "Due date", allow_empty=True),
        "updates[*].start_date": Field(DATE, "Start date", allow_empty=True),
    },
    "add_comment": {"body_text": Field(BODY, "Comment", required=True)},
    "update_comment": {"body_text": Field(BODY, "Comment", required=True)},
    # --- notes ---
    "create_note": {
        "title": _title(_NOTE_TITLE),
        "content_text": Field(BODY, "Content"),
    },
    "update_note": {
        "title": _title(_NOTE_TITLE),
        "content_text": Field(BODY, "Content"),
    },
    # --- to-dos ---
    # On create `remind_at: ''` is "no reminder", which is what the card would
    # show. On update `''` is "leave the reminder alone", so it is refused. A
    # to-do's `target_date` is refused empty on both: on create it falls back to
    # today, on update it means "do not move".
    "create_todo_item": {
        "title": _title(_TODO_TITLE),
        "notes_text": Field(BODY, "Notes"),
        "target_date": Field(DATE, "Day"),
        "remind_at": Field(REMIND_AT, "Reminder", allow_empty=True),
    },
    "update_todo_item": {
        "title": _title(_TODO_TITLE),
        "notes_text": Field(BODY, "Notes"),
        "target_date": Field(DATE, "Day"),
        "remind_at": Field(REMIND_AT, "Reminder"),
    },
    # --- nothing editable: every other approval-gated tool ---
    # Plans (`create_task_plan` rows), people and ids, structure, sharing,
    # membership, calendars, and every delete / restore / approve / decline.
    "create_task_plan": _NONE,
    "assign_task": _NONE,
    "delete_comment": _NONE,
    "restore_task_body": _NONE,
    "add_task_delivery": _NONE,
    "remove_task_delivery": _NONE,
    "update_task_dependencies": _NONE,
    "manage_milestone": _NONE,
    "delete_milestone": _NONE,
    "manage_sprint": _NONE,
    "delete_sprint": _NONE,
    "manage_project": _NONE,
    "manage_project_members": _NONE,
    "add_project_tag": _NONE,
    "manage_project_labels": _NONE,
    "approve_join_request": _NONE,
    "decline_join_request": _NONE,
    "delete_project": _NONE,
    "create_calendar_event": _NONE,
    "update_calendar_event": _NONE,
    "delete_calendar_event": _NONE,
    "delete_todo_item": _NONE,
    "manage_note_folder": _NONE,
    "favorite_note": _NONE,
    "delete_note": _NONE,
    "delete_note_folder": _NONE,
    "manage_note_sharing": _NONE,
    "restore_note_version": _NONE,
}


# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #

# `name`, or `name[row].field`. One row index, no deeper, no leading zeros, ASCII
# digits only (`\d` also matches other scripts' digits): a row has exactly one
# spelling, so a path can be compared as a string.
_PATH = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(0|[1-9][0-9]{0,3})\]\.([A-Za-z_][A-Za-z0-9_]*))?"
)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_Parsed = tuple[str, int | None, str | None]  # (name, row, field)


def parse_path(path: Any) -> _Parsed | None:
    """`(name, None, None)` for `title`, `(name, 3, field)` for `updates[3].priority`,
    None for anything that is not one of those two shapes."""
    if not isinstance(path, str):
        return None
    match = _PATH.fullmatch(path)
    if match is None:
        return None
    row = match.group(2)
    return (match.group(1), None if row is None else int(row), match.group(3))


def _template(parsed: _Parsed) -> str:
    name, row, field = parsed
    return name if row is None else f"{name}[*].{field}"


def _lookup(arguments: dict[str, Any], parsed: _Parsed) -> tuple[bool, Any]:
    """`(True, value)` when the call carries a value at `parsed`, else
    `(False, None)`. A key holding null is not carried: the tools read it as
    "left out"."""
    name, row, field = parsed
    if row is None:
        value = arguments.get(name)
        return (value is not None, value)
    rows = arguments.get(name)
    if not isinstance(rows, list) or row >= len(rows) or not isinstance(rows[row], dict):
        return (False, None)
    value = rows[row].get(field)
    return (value is not None, value)


def editable_paths(tool_name: str, arguments: dict[str, Any] | None) -> list[str]:
    """The allow-listed paths this call carries: what the card may render as
    editable, and the only paths `decide` accepts. Flat paths in the allow-list's
    order, then bulk row paths row by row."""
    fields = EDITABLE_FIELDS.get(tool_name) or {}
    arguments = arguments if isinstance(arguments, dict) else {}
    flat: list[str] = []
    rows: list[tuple[int, int, str]] = []
    for order, template in enumerate(fields):
        if "[*]." not in template:
            if arguments.get(template) is not None:
                flat.append(template)
            continue
        name, field = template.split("[*].")
        entries = arguments.get(name)
        if not isinstance(entries, list):
            continue
        for index, row in enumerate(entries):
            if isinstance(row, dict) and row.get(field) is not None:
                rows.append((index, order, f"{name}[{index}].{field}"))
    return flat + [path for _index, _order, path in sorted(rows)]


# --------------------------------------------------------------------------- #
# Checking a patch                                                            #
# --------------------------------------------------------------------------- #


class EditRejected(Exception):
    """A patch the allow-list or a field's rule refuses: `field_errors` maps each
    offending path to a sentence for the person."""

    def __init__(self, field_errors: dict[str, str]):
        super().__init__("; ".join(f"{path}: {msg}" for path, msg in field_errors.items()))
        self.field_errors = field_errors


def _schema_node(tool_name: str, template: str) -> dict[str, Any] | None:
    """The tool's own `parameters_schema` entry for `template`, or None."""
    schema = getattr(REGISTRY.get(tool_name), "parameters_schema", None)
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties") or {}
    if "[*]." not in template:
        node = properties.get(template)
    else:
        name, field = template.split("[*].")
        items = (properties.get(name) or {}).get("items") or {}
        node = (items.get("properties") or {}).get(field)
    return node if isinstance(node, dict) else None


def _unsavable(value: str) -> bool:
    """Text the database refuses (a NUL, a lone surrogate). The tools turn that
    into "Failed to ..." after the approval is spent."""
    if "\x00" in value:
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _type_problem(node: dict[str, Any], value: Any, label: str) -> str | None:
    """The generic check, from the tool's declared schema: the value is the
    declared type. Enums are checked by `_check_value`, which knows what a field
    excludes."""
    declared = str(node.get("type") or "").upper()
    if declared == "STRING":
        if not isinstance(value, str):
            return f"{label} must be text."
        if _unsavable(value):
            return f"{label} contains a character that can't be saved."
    elif declared == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{label} must be a whole number."
    elif declared == "NUMBER":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{label} must be a number."
    elif declared == "BOOLEAN":
        if not isinstance(value, bool):
            return f"{label} must be true or false."
    elif declared == "ARRAY":
        if not isinstance(value, list):
            return f"{label} must be a list."
        item = node.get("items")
        if isinstance(item, dict):
            for entry in value:
                problem = _type_problem(item, entry, label)
                if problem:
                    return problem
    else:
        # An object, or a type this module does not know: not editable as a value.
        return f"{label} can't be edited here."
    return None


def _check_value(
    tool_name: str, template: str, spec: Field, value: Any, ctx: ToolContext
) -> str | None:
    """Why `value` is not acceptable for `spec`, in words for a person, or None."""
    label = spec.label
    if value is None:
        return f"{label}: enter a value."
    node = _schema_node(tool_name, template)
    if node is None:
        # The allow-list names a field the tool does not declare. A test makes
        # that impossible to merge; here it fails closed.
        return f"{label} can't be edited here."
    problem = _type_problem(node, value, label)
    if problem:
        return problem

    if spec.kind in (TEXT, BODY):
        if spec.required and not value.strip():
            return f"{label} can't be empty."
        if spec.max_length is not None and len(value.strip()) > spec.max_length:
            return f"{label} is too long (at most {spec.max_length} characters)."
        if spec.kind == BODY:
            try:
                check_body_size(value, template)
            except ToolError:
                limit = max_body_bytes()
                size = f"{limit // 1024} KB" if limit >= 1024 else f"{limit} bytes"
                return f"{label} is too long (the limit is {size})."
    elif spec.kind == ENUM:
        allowed = [v for v in node.get("enum") or [] if v not in spec.exclude]
        if value not in allowed:
            return f"{label}: choose one of {', '.join(allowed)}."
    elif spec.kind == DATE:
        if value == "":
            return None if spec.allow_empty else f"{label}: use a date like 2026-11-20."
        if not _ISO_DATE.fullmatch(value):
            return f"{label}: use a date like 2026-11-20 (year-month-day)."
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"{label}: that date doesn't exist."
    elif spec.kind == REMIND_AT:
        if value == "":
            return None if spec.allow_empty else f"{label}: enter a date and time."
        try:
            resolve_remind_at(remind_at=value, zone=zone_for_user_id(ctx.user_id))
        except ReminderInputError:
            return (
                f"{label}: enter a time in the future, within the next year, like 2026-11-06T09:00."
            )
    # TAGS: a list of text, checked above. Whether each name exists in the
    # task's project is the tool's to answer, at run time.
    return None


def check_edits(
    tool_name: str,
    arguments: dict[str, Any] | None,
    patch: dict[Any, Any],
    ctx: ToolContext,
) -> dict[str, str]:
    """`{path: message}` for every entry of `patch` that cannot be applied; empty
    when the whole patch can. `tool_name` and `arguments` are the PENDING STEP's,
    never the request's."""
    arguments = arguments if isinstance(arguments, dict) else {}
    fields = EDITABLE_FIELDS.get(tool_name) or {}
    carried = set(editable_paths(tool_name, arguments))
    errors: dict[str, str] = {}
    for path, value in patch.items():
        key = path if isinstance(path, str) else repr(path)
        parsed = parse_path(path)
        template = _template(parsed) if parsed is not None else None
        spec = fields.get(template) if template is not None else None
        if spec is None:
            errors[key] = "This can't be edited."
        elif path not in carried:
            errors[key] = "This isn't part of the proposed action, so it can't be edited."
        else:
            message = _check_value(tool_name, template, spec, value, ctx)
            if message:
                errors[key] = message
    return errors


def prepare(
    tool_name: str,
    arguments: dict[str, Any] | None,
    patch: dict[Any, Any],
    ctx: ToolContext,
) -> dict[str, Any]:
    """The patch to apply: `patch` with every entry that only restates the
    proposal dropped (an edit that changes nothing is not an edit). Raises
    `EditRejected` with every offending path when any entry cannot be applied.

    "Restates the proposal" is judged against what the card SHOWED
    (`for_card`: the proposal with a fence the model copied taken off), as well as
    against the stored value, because the editor starts from the former."""
    errors = check_edits(tool_name, arguments, patch, ctx)
    if errors:
        raise EditRejected(errors)
    arguments = arguments if isinstance(arguments, dict) else {}
    kept: dict[str, Any] = {}
    for path, value in patch.items():
        original = _lookup(arguments, parse_path(path))[1]
        if value != original and value != unfence_arguments(original):
            kept[path] = value
    return kept


def for_card(
    friendly: dict[str, Any], arguments: dict[str, Any] | None, paths: list[str]
) -> dict[str, Any]:
    """`friendly` (the card's arguments) with the value at each of `paths` as the
    tool would receive it: a fence the MODEL copied off a read result taken off,
    and its escape undone (`unfence_arguments`).

    The model is told to send a note's or a task's text back verbatim, and the
    read tools hand it that text inside a `<workspace_content>` fence, so a proposal
    can carry the fence. The tool strips it; an editor that began from the stored
    value would show the tags, and a person who fixed one word would then save them
    as text (their edit is escaped, not unfenced). So the editable values are shown
    clean, and the stored proposal, which is what the model is replayed, is untouched.
    A value with nothing to take off is returned as it was.
    """
    shown = copy.deepcopy(friendly)
    for path in paths:
        parsed = parse_path(path)
        if parsed is None:
            continue
        found, raw = _lookup(arguments if isinstance(arguments, dict) else {}, parsed)
        if not found:
            continue
        clean = unfence_arguments(raw)
        if clean == raw:
            continue
        name, row, field = parsed
        if row is None:
            shown[name] = clean
        else:
            rows = shown.get(name)
            if isinstance(rows, list) and row < len(rows) and isinstance(rows[row], dict):
                rows[row][field] = clean
    return shown


# --------------------------------------------------------------------------- #
# Applying it                                                                 #
# --------------------------------------------------------------------------- #


def apply_edits(
    arguments: dict[str, Any] | None, patch: dict[str, Any], *, for_run: bool
) -> dict[str, Any]:
    """The EFFECTIVE arguments: a copy of `arguments` with `patch` merged in.
    `arguments` itself is never touched (it is the model's own call, replayed to it).

    `for_run=True` is what goes to `tool.run`: every edited string is escaped
    (`escape_user_arguments`) so `with_unfenced_arguments` hands the tool exactly
    what was typed. `for_run=False` is the person's text as typed, for the card,
    the events and the audit. A path that is not in `arguments` is skipped: the
    caller has already refused it.
    """
    merged = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}
    for path, value in patch.items():
        parsed = parse_path(path)
        if parsed is None or not _lookup(merged, parsed)[0]:
            continue
        new = escape_user_arguments(copy.deepcopy(value)) if for_run else copy.deepcopy(value)
        name, row, field = parsed
        if row is None:
            merged[name] = new
        else:
            merged[name][row][field] = new
    return merged

"""A project's custom task fields, by NAME, for the agent tools (MIP-877).

A task stores its custom values in `custom_field_values`, a map keyed by the
field's numeric id, and a tag field's value is a list of option ids. Neither
means anything to an agent or to someone reading the result: the UI shows
"Customer: Acme", not "17: opt-1699-ab". The tools speak names and labels, and
this module is the one place that converts between the two.

Reading: `read_custom_fields` turns the stored map into one entry per field
that has a value, `{name, type, value}` (`values` for a tag field, which holds
several). Only the fields the task's own project defines are shown; a value
whose field was deleted is left on the row on purpose and is invisible here, as
it is in the UI.

Naming a field: `resolve_field` finds it by name within ONE project. An exact
name wins; otherwise a case-insensitive match is accepted when it picks out
exactly one field (the database only forbids two identical names, so "Owner"
and "owner" can both exist and must count as ambiguous). An unknown or
ambiguous name is refused with the project's field names in the message.

Writing: `plan_custom_fields` turns the caller's `[{name, value | values}]`
into the full stored map, validating each value against its field's type and
refusing what the UI's pickers could not have produced. It changes nothing;
the caller saves the map it returns.

What is user-typed is fenced as workspace content, as in the sibling reads: the
field name, a text value, a tag option's label. A fence copied back in as part
of a name is taken off, because `unfence_arguments` only strips argument VALUES,
never the keys or names a tool matches on.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from origin.models.project.prj_models import ProjectCustomField
from origin.search_engine.agent.model_fence import unfence_arguments
from origin.search_engine.agent.tools.base import ToolError, wrap_workspace_content
from origin.search_engine.agent.tools.task_people import person_id
from origin.services.custom_fields import (
    MAX_ENTRIES,
    MAX_TEXT_LEN,
    parse_stored_date,
    sanitize_custom_field_values,
)


def project_fields(project_id: int | None) -> list[ProjectCustomField]:
    """The project's custom fields in the order the UI lists them."""
    if not project_id:
        return []
    return list(
        ProjectCustomField.objects.filter(project_id=project_id).order_by("sort_order", "field_id")
    )


def clean_name(raw: Any) -> str:
    """A field name or option label as the caller sent it: a copied fence
    taken off, surrounding space trimmed."""
    return unfence_arguments(str(raw)).strip()


def names_of(fields: list[ProjectCustomField]) -> str:
    """The field names, for an error message that tells the caller what is valid."""
    return ", ".join(repr(f.field_name) for f in fields) or "none"


def resolve_field(
    raw_name: Any, fields: list[ProjectCustomField], *, where: str = "`custom_fields`"
) -> ProjectCustomField:
    """The field called `raw_name` among `fields` (one project's), or a
    ToolError that lists the valid names."""
    name = clean_name(raw_name)
    for field in fields:
        if field.field_name == name:
            return field
    folded = [f for f in fields if f.field_name.casefold() == name.casefold()]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise ToolError(
            f"{where}: {name!r} matches more than one field ({names_of(folded)}). "
            "Use the exact name."
        )
    raise ToolError(
        f"{where}: this project has no custom field {name!r}. "
        f"Its custom fields: {names_of(fields)}."
    )


def option_labels(field: ProjectCustomField) -> dict[str, str]:
    """`option id -> label` for a tag field."""
    return {
        str(opt.get("id")): str(opt.get("label"))
        for opt in (field.options or [])
        if isinstance(opt, dict) and opt.get("id") is not None and opt.get("label") is not None
    }


def field_definitions(fields: list[ProjectCustomField]) -> list[dict[str, Any]]:
    """What `list_project_tags` returns for the project's fields: the name,
    the type (`tag`, `text`, `date` or `member`) and, for a tag field, the
    option labels a value can be chosen from."""
    out = []
    for field in fields:
        row: dict[str, Any] = {
            "field_name": wrap_workspace_content(field.field_name),
            "type": field.field_type,
        }
        if field.field_type == "tag":
            row["options"] = [
                wrap_workspace_content(label) for label in option_labels(field).values()
            ]
        out.append(row)
    return out


def stored_member_ids(values: Any, fields: list[ProjectCustomField]) -> set[str]:
    """The user ids held by the member fields among `values`, so a caller can
    look their usernames up in one query."""
    if not isinstance(values, dict):
        return set()
    return {
        value
        for field in fields
        if field.field_type == "member"
        and isinstance(value := values.get(str(field.field_id)), str)
        and value
    }


def read_custom_fields(
    values: Any,
    fields: list[ProjectCustomField],
    usernames: dict[str, str] | None = None,
    *,
    fence: bool = True,
) -> list[dict[str, Any]]:
    """The stored `values` (keyed by field id) as a list with one entry per field
    that has a value, in the UI's order.

    A tag field's entry carries `values` (option labels; an option deleted since
    is dropped, and a field left with none is not listed). The others carry
    `value`: the text, an ISO date, or a user id, with the person's `username`
    beside it when `usernames` can name them. A value of the wrong shape for its
    field is skipped rather than guessed at. `fence=False` gives the same
    entries with the text bare, for a label shown to the person who asked (the
    approval card) rather than to a model.
    """
    wrap = wrap_workspace_content if fence else (lambda text: text)
    if not isinstance(values, dict):
        return []
    usernames = usernames or {}
    out: list[dict[str, Any]] = []
    for field in fields:
        stored = values.get(str(field.field_id))
        if stored in (None, "", []):
            continue
        entry: dict[str, Any] = {
            "name": wrap(field.field_name),
            "type": field.field_type,
        }
        if field.field_type == "tag":
            labels = option_labels(field)
            ids = stored if isinstance(stored, list) else [stored]
            chosen = [labels[str(i)] for i in ids if str(i) in labels]
            if not chosen:
                continue
            entry["values"] = [wrap(label) for label in chosen]
        elif not isinstance(stored, str):
            continue
        elif field.field_type == "date":
            try:
                entry["value"] = date.fromisoformat(stored).isoformat()
            except ValueError:
                # Not a date, so not data the product wrote: fence it.
                entry["value"] = wrap(stored)
        elif field.field_type == "member":
            entry["value"] = stored
            name = usernames.get(stored)
            entry["username"] = wrap(name) if name else None
        else:
            entry["value"] = wrap(stored)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

_ENTRY_KEYS = {"name", "value", "values"}

# The `custom_fields` argument of create_task / update_task. A list of objects
# rather than a name -> value map: a function declaration can't describe a map
# with open keys, and a name used as a key could not be unfenced.
CUSTOM_FIELDS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The custom field's name, from list_project_tags.",
            },
            "value": {
                "type": "STRING",
                "description": (
                    "The new value: text, a YYYY-MM-DD date, or a user id (member "
                    "field). '' clears the field. A tag field takes `values`."
                ),
            },
            "values": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "For a tag field only: the option labels to select, replacing "
                    "the current selection. [] clears it."
                ),
            },
        },
        "required": ["name"],
    },
    "description": (
        "Custom fields to set, by name: [{name, value}], or [{name, values}] for a tag "
        "field. Only the fields named change. A tag option must be one of the field's "
        "options, a date YYYY-MM-DD, a member someone on the team or project. Omit to "
        "leave all unchanged."
    ),
}


def parse_iso_date(raw: Any, where: str) -> date:
    """`YYYY-MM-DD` only (the rule is `custom_fields.parse_stored_date`, shared
    with the REST PUT)."""
    parsed = parse_stored_date(raw)
    if parsed is None:
        raise ToolError(f"{where} must be a date as YYYY-MM-DD, such as 2026-11-20 (got {raw!r}).")
    return parsed


def _tag_option_ids(field: ProjectCustomField, picked: list[str], where: str) -> list[str]:
    """Option ids for the labels (or ids) in `picked`, in the order given, each
    once. An exact label wins; then an option id; then a case-insensitive label
    when it picks out exactly one option."""
    options = option_labels(field)
    out: list[str] = []
    unknown: list[str] = []
    for item in picked:
        label = clean_name(item)
        match = [i for i, text in options.items() if text == label]
        if not match and label in options:
            match = [label]
        if not match:
            match = [i for i, text in options.items() if text.casefold() == label.casefold()]
        if len(match) == 1:
            if match[0] not in out:
                out.append(match[0])
        else:
            unknown.append(label)
    if unknown:
        choices = ", ".join(repr(text) for text in options.values()) or "none"
        raise ToolError(
            f"{where}: {', '.join(repr(u) for u in unknown)} is not an option of the tag field "
            f"{field.field_name!r}. Its options: {choices}."
        )
    return out


def _one_value(field: ProjectCustomField, entry: dict[str, Any], project: Any, where: str) -> Any:
    """The stored value for one entry, or None to clear the field."""
    has_value, has_values = "value" in entry, "values" in entry
    if has_value and has_values:
        raise ToolError(f"{where}: send `value` or `values`, not both.")
    if not (has_value or has_values):
        raise ToolError(
            f"{where}: say what to set: `value` (or `values` for a tag field). '' clears the field."
        )
    raw = entry["values"] if has_values else entry["value"]
    if field.field_type == "tag":
        if raw is None or raw == "":
            return None
        picked = [raw] if isinstance(raw, str) else raw
        if not isinstance(picked, list) or not all(isinstance(p, str) for p in picked):
            raise ToolError(
                f"{where}: a tag field takes `values`, a list of option labels (got {raw!r})."
            )
        return _tag_option_ids(field, picked, where) or None
    if has_values:
        raise ToolError(
            f"{where}: only a tag field takes `values`; {field.field_name!r} is a "
            f"{field.field_type} field. Send `value`."
        )
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ToolError(
            f"{where}: `value` for {field.field_name!r} must be text (got {type(raw).__name__})."
        )
    if field.field_type == "text":
        if len(raw) > MAX_TEXT_LEN:
            raise ToolError(
                f"{where}: the text for {field.field_name!r} is {len(raw)} characters; "
                f"the limit is {MAX_TEXT_LEN}. Nothing was changed."
            )
        return raw
    if field.field_type == "date":
        return parse_iso_date(raw, f"{where}: the date for {field.field_name!r}").isoformat()
    if field.field_type == "member":
        return person_id(raw, project, where)
    raise ToolError(f"{where}: {field.field_name!r} has a type this tool cannot set.")


def plan_custom_fields(
    raw: Any,
    fields: list[ProjectCustomField],
    stored: Any,
    project: Any,
    *,
    where: str = "`custom_fields`",
) -> dict[str, Any]:
    """The task's whole new `custom_field_values` after applying `raw`.

    `raw` is a list of `{name, value}` objects (`values` for a tag field): the
    fields to set, by name, with `''` (or `[]`) clearing one. The stored map is
    the base, so a field not named keeps its value, and so does a value whose
    field is gone (the product leaves those on the row on purpose). The result
    goes through the same `sanitize_custom_field_values` the REST PUT applies.

    Every entry is checked before anything is returned, and nothing is written
    here: the caller saves the result, or, on a refusal, nothing at all.
    """
    if not isinstance(raw, list):
        raise ToolError(
            f"{where} must be a list of {{name, value}} objects, such as "
            '[{"name": "Customer", "value": "Acme"}] '
            f"(got {type(raw).__name__})."
        )
    merged = dict(stored) if isinstance(stored, dict) else {}
    seen: set[int] = set()
    for i, entry in enumerate(raw):
        here = f"{where}[{i}]"
        if not isinstance(entry, dict):
            raise ToolError(f"{here} must be an object {{name, value}} (got {entry!r}).")
        if extra := sorted(str(k) for k in entry if k not in _ENTRY_KEYS):
            raise ToolError(
                f"{here} has no `{extra[0]}`: an entry is {{name, value}}, or {{name, values}} "
                "for a tag field."
            )
        if entry.get("name") in (None, ""):
            raise ToolError(f"{here}: `name` is required: the custom field's name.")
        field = resolve_field(entry["name"], fields, where=here)
        if field.field_id in seen:
            raise ToolError(f"{here}: {field.field_name!r} is listed twice.")
        seen.add(field.field_id)
        value = _one_value(field, entry, project, here)
        if value is None:
            merged.pop(str(field.field_id), None)
        else:
            merged[str(field.field_id)] = value
    if len(merged) > MAX_ENTRIES:
        raise ToolError(f"{where}: a task can hold at most {MAX_ENTRIES} custom values.")
    return sanitize_custom_field_values(merged) or {}

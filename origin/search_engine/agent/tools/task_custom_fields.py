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
) -> list[dict[str, Any]]:
    """The stored `values` (keyed by field id) as a list with one entry per field
    that has a value, in the UI's order.

    A tag field's entry carries `values` (option labels; an option deleted since
    is dropped, and a field left with none is not listed). The others carry
    `value`: the text, an ISO date, or a user id, with the person's `username`
    beside it when `usernames` can name them. A value of the wrong shape for its
    field is skipped rather than guessed at.
    """
    if not isinstance(values, dict):
        return []
    usernames = usernames or {}
    out: list[dict[str, Any]] = []
    for field in fields:
        stored = values.get(str(field.field_id))
        if stored in (None, "", []):
            continue
        entry: dict[str, Any] = {
            "name": wrap_workspace_content(field.field_name),
            "type": field.field_type,
        }
        if field.field_type == "tag":
            labels = option_labels(field)
            ids = stored if isinstance(stored, list) else [stored]
            chosen = [labels[str(i)] for i in ids if str(i) in labels]
            if not chosen:
                continue
            entry["values"] = [wrap_workspace_content(label) for label in chosen]
        elif not isinstance(stored, str):
            continue
        elif field.field_type == "date":
            try:
                entry["value"] = date.fromisoformat(stored).isoformat()
            except ValueError:
                # Not a date, so not data the product wrote: fence it.
                entry["value"] = wrap_workspace_content(stored)
        elif field.field_type == "member":
            entry["value"] = stored
            name = usernames.get(stored)
            entry["username"] = wrap_workspace_content(name) if name else None
        else:
            entry["value"] = wrap_workspace_content(stored)
        out.append(entry)
    return out

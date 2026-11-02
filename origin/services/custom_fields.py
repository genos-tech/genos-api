"""Shape validation for per-project custom task fields.

Two write surfaces share this module: the task create/update endpoints
(`TaskMasterView`) and the milestone endpoints (which store values on
the milestone's backing task row). Both accept a `custom_field_values`
JSON map and must not let arbitrarily-shaped JSON land in
`TaskMaster.custom_field_values`.

`sanitize_custom_field_values` is SHAPE-only, deliberately. Task `tags`
set the precedent: the client is authoritative for content, the server
only refuses payloads that can't be a value map at all. Field-id /
option-id existence is NOT checked there — a value written moments
before a teammate deleted the field is fine (readers drop unknown ids),
and hard-failing a task save on that race would be hostile.

What a value MEANS is checked separately, by `refuse_changed_values`
(MIP-913), and only for values that CHANGED. The app re-sends the whole
map on every save, so a tag option deleted since, or a person who has
left the team since, must not fail every later save of the task; and a
value for a field that no longer exists is still ignored. A value that is
new or different has to be one a picker could have produced: a tag
option of the field, a `YYYY-MM-DD` date, a person who may be put on the
task. The agent tools (`task_custom_fields`) apply the same rules to what
they are given by name.

Value shapes (keyed by str(ProjectCustomField.field_id)):

    tag     list[str] of option ids
    text    str
    date    str ("YYYY-MM-DD"; sanitizing does not parse it)
    member  str user id

so every value is `str | list[str]`. `None` / empty values mean "clear"
and are dropped from the stored map.
"""

import re
import uuid
from datetime import date

from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectCustomField
from origin.services.task_collaborators import may_name_person

# Caps are generous sanity bounds, not product limits — they exist so a
# buggy or malicious client can't grow a task row without bound.
MAX_ENTRIES = 100
MAX_TEXT_LEN = 4000
MAX_LIST_LEN = 50
MAX_LIST_ITEM_LEN = 100

CUSTOM_FIELD_TYPES = ("tag", "text", "date", "member")

# Option-list bounds for ProjectCustomFieldsView.
MAX_OPTIONS = 50
MAX_OPTION_LABEL_LEN = 40
MAX_OPTION_ID_LEN = 64
MAX_OPTION_COLOR_LEN = 24


def sanitize_custom_field_values(raw):
    """Coerce a client-sent value map into storable shape.

    Returns the cleaned dict, or ``None`` when `raw` is not a dict at
    all (the caller should 400). Individual malformed ENTRIES are
    dropped rather than failing the whole save — same spirit as the
    reader side dropping orphaned ids.
    """

    if not isinstance(raw, dict):
        return None

    cleaned = {}
    for key, value in raw.items():
        if len(cleaned) >= MAX_ENTRIES:
            break
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            # "clear" — represented by absence in the stored map.
            continue
        if isinstance(value, str):
            trimmed = value[:MAX_TEXT_LEN]
            if trimmed == "":
                continue
            cleaned[key] = trimmed
        elif isinstance(value, list):
            items = [
                item[:MAX_LIST_ITEM_LEN]
                for item in value[:MAX_LIST_LEN]
                if isinstance(item, str) and item
            ]
            if not items:
                continue
            cleaned[key] = items
        # Any other type (dict, number, bool, …) is silently dropped.
    return cleaned


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_stored_date(raw) -> date | None:
    """`YYYY-MM-DD` only, or None. `date.fromisoformat` also takes `20261120`
    and `2026-W47-5`, which no date picker produces and a reader would not
    expect to find stored."""
    if isinstance(raw, str) and _ISO_DATE.fullmatch(raw.strip()):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    return None


def option_ids(field) -> set[str]:
    """The ids of a tag field's options."""
    return {
        str(opt["id"])
        for opt in (field.options or [])
        if isinstance(opt, dict) and opt.get("id") is not None
    }


def _person_is_allowed(project, raw) -> bool:
    """A person who exists, has not deleted their account, and may be put on
    a task in `project` (`may_name_person`: its members, or its team's)."""
    try:
        user_id = str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError):
        return False
    user = CustomUser.objects.filter(id=user_id).first()
    return user is not None and not user.is_deleted and may_name_person(project, user_id)


def invalid_value_reason(field, value, project, *, already=()) -> str | None:
    """Why `value` can't be stored in `field`, or None.

    `value` is in stored form (`str | list[str]`, ids not labels). `already` is
    what the field held before, for a tag field: an option that was in the list
    is not judged again, so a deleted option doesn't fail a save that merely
    keeps it. A tag value is a list; every other type is a single string.
    """
    kind = field.field_type
    if kind == "tag":
        if not isinstance(value, list):
            return "takes a list of option ids"
        known = option_ids(field)
        unknown = [v for v in value if v not in known and v not in already]
        if unknown:
            return f"has no option {unknown[0]!r}"
        return None
    if not isinstance(value, str):
        return "takes a single value, not a list"
    if kind == "date":
        return None if parse_stored_date(value) else "must be a date as YYYY-MM-DD"
    if kind == "member":
        if _person_is_allowed(project, value):
            return None
        return "must be a member of this project or its team"
    return None


def refuse_changed_values(raw, cleaned, stored, project) -> str | None:
    """Why a task save may not store `cleaned` (what `sanitize_custom_field_values`
    made of `raw`) given the `stored` map, or None.

    Only a value that is new or different is judged (see the module docstring),
    and a value for a field the project no longer has is ignored, as it always
    was. For a field the project has, what sanitizing would DROP silently is
    refused instead: a number or a boolean, or a list item that isn't text, can
    never come from a picker, and a write that reports success while keeping
    nothing is the worse answer.
    """
    stored = stored if isinstance(stored, dict) else {}

    def project_fields():
        return {
            str(f.field_id): f
            for f in ProjectCustomField.objects.filter(
                project_id=getattr(project, "project_id", None)
            )
        }

    fields = None
    if isinstance(raw, dict):
        odd = [
            key
            for key, value in raw.items()
            if not (
                value is None
                or isinstance(value, str)
                or (isinstance(value, list) and all(isinstance(item, str) for item in value))
            )
        ]
        if odd:
            fields = project_fields()
            for key in odd:
                # An entry that names no field of this project is junk and is
                # dropped, as it always was. One that names a real field is a
                # value no picker could have produced.
                if key in fields:
                    return (
                        f"Custom field {fields[key].field_name!r} takes text, "
                        "or a list of option ids."
                    )

    changed = {k: v for k, v in (cleaned or {}).items() if v != stored.get(k)}
    if not changed:
        return None
    if fields is None:
        fields = project_fields()
    for key, value in changed.items():
        field = fields.get(key)
        if field is None:
            continue
        before = stored.get(key)
        reason = invalid_value_reason(
            field, value, project, already=before if isinstance(before, list) else ()
        )
        if reason:
            return f"Custom field {field.field_name!r} {reason}."
    return None


def validate_field_options(options):
    """Validate a tag-field option list. Returns an error string or None.

    Every option is `{"id", "label", "color"}` (+ optional "textColor").
    Ids are opaque client-minted strings and must be unique within the
    field — values reference options BY ID, so a duplicate id would make
    stored values ambiguous.
    """

    if not isinstance(options, list):
        return "options must be a list."
    if len(options) > MAX_OPTIONS:
        return f"options cannot exceed {MAX_OPTIONS} entries."
    seen_ids = set()
    for opt in options:
        if not isinstance(opt, dict):
            return "Each option must be an object."
        opt_id = opt.get("id")
        label = opt.get("label")
        if not isinstance(opt_id, str) or not opt_id or len(opt_id) > MAX_OPTION_ID_LEN:
            return "Each option needs a non-empty string 'id'."
        if opt_id in seen_ids:
            return f"Duplicate option id '{opt_id}'."
        seen_ids.add(opt_id)
        if not isinstance(label, str) or not label.strip():
            return "Each option needs a non-empty 'label'."
        if len(label) > MAX_OPTION_LABEL_LEN:
            return f"Option labels cannot exceed {MAX_OPTION_LABEL_LEN} characters."
        for color_key in ("color", "textColor"):
            color = opt.get(color_key)
            if color is not None and (
                not isinstance(color, str) or len(color) > MAX_OPTION_COLOR_LEN
            ):
                return f"Option '{color_key}' must be a short string."
        unknown = set(opt.keys()) - {"id", "label", "color", "textColor"}
        if unknown:
            return f"Unknown option key '{sorted(unknown)[0]}'."
    return None

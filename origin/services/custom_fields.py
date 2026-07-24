"""Shape validation for per-project custom task fields.

Two write surfaces share this module: the task create/update endpoints
(`TaskMasterView`) and the milestone endpoints (which store values on
the milestone's backing task row). Both accept a `custom_field_values`
JSON map and must not let arbitrarily-shaped JSON land in
`TaskMaster.custom_field_values`.

Validation is SHAPE-only, deliberately. Task `tags` set the precedent:
the client is authoritative for content, the server only refuses
payloads that can't be a value map at all. Field-id / option-id
existence is NOT checked here — a value written moments before a
teammate deleted the field is fine (readers drop unknown ids), and
hard-failing a task save on that race would be hostile.

Value shapes (keyed by str(ProjectCustomField.field_id)):

    tag     list[str] of option ids
    text    str
    date    str ("YYYY-MM-DD" by convention; not parsed here)
    member  str user id

so every value is `str | list[str]`. `None` / empty values mean "clear"
and are dropped from the stored map.
"""

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

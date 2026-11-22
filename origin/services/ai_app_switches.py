"""The switches for what a member's AI apps did over MCP (MIP-1432, MIP-1452).

One switch per action: what happened (the notice's `verb`) to what (its `kind`). Each is a key in
`NotificationPreference.category_settings`, under the group master `enable_ai_apps`. The same keys
decide whether the notice is MADE (`mcp_notice`), whether it is pushed (`webpush_gating`) and what
the push is filed under (`webpush_dispatch`), so the client's registry
(`categories.ts`) and this table must agree.

Only the RISKY actions start on (MIP-2009, decision D4 of the agent audit trail): a delete, a change
to who can open a note or who is in a project, a burst of refused calls, a new app or API key
connected (MIP-2063), and any call that changes 10 or more items at once (`BULK_THRESHOLD`, whatever its action). Every other write
makes no feed row, toast or push by default: about 23 a day for one person was noise, and what the
app did shows in the Today card instead. A member who set a switch (either way) keeps that choice;
the new defaults reach only a switch they never touched.
"""

from __future__ import annotations

from typing import Any

# (verb, kind) -> the switch. A `comment` kind is a comment on a task; ("commented", "note") is a
# comment thread started or answered on a note (MIP-1669).
ACTIONS: dict[tuple[str, str], str] = {
    ("created", "task"): "ai_app_task_created",
    ("status", "task"): "ai_app_task_status",
    ("commented", "comment"): "ai_app_task_commented",
    ("commented", "note"): "ai_app_note_commented",
    ("deleted", "comment"): "ai_app_comment_deleted",
    ("created", "milestone"): "ai_app_milestone_created",
    ("status", "milestone"): "ai_app_milestone_status",
    ("deleted", "milestone"): "ai_app_milestone_deleted",
    ("created", "note"): "ai_app_note_created",
    ("edited", "note"): "ai_app_note_edited",
    ("deleted", "note"): "ai_app_note_deleted",
    ("created", "todo"): "ai_app_todo_created",
    ("deleted", "todo"): "ai_app_todo_deleted",
    # The risky kinds that are not a write of an item (MIP-2063, decision D4 of MIP-1998):
    # who can open a note, who is in a project, a burst of refused calls, a new app or key.
    ("sharing", "note"): "ai_app_note_sharing",
    ("members", "project"): "ai_app_project_members",
    ("refused", "burst"): "ai_app_refusal_burst",
    ("connected", "app"): "ai_app_app_connected",
}

KEYS: tuple[str, ...] = tuple(ACTIONS.values())

# A call that changes this many items at once is a bulk change, which alerts whatever its action.
BULK_THRESHOLD = 10

# The risky actions, on until the member turns them off.
ON_BY_DEFAULT = frozenset(
    {
        "ai_app_comment_deleted",
        "ai_app_milestone_deleted",
        "ai_app_note_deleted",
        "ai_app_todo_deleted",
        "ai_app_note_sharing",
        "ai_app_project_members",
        "ai_app_refusal_burst",
        "ai_app_app_connected",
    }
)

DEFAULTS: dict[str, bool] = {key: key in ON_BY_DEFAULT for key in KEYS}


def key_for(verb: Any, kind: Any) -> str | None:
    """The switch for an action, or None for one no rule produces (only the group master then
    decides)."""
    return ACTIONS.get((str(verb or ""), str(kind or "")))


def is_on(category_settings: dict[str, Any] | None, key: str | None) -> bool:
    if key is None:
        return True
    return bool((category_settings or {}).get(key, DEFAULTS.get(key, True)))


def is_bulk(count: Any) -> bool:
    try:
        return int(count) >= BULK_THRESHOLD
    except (TypeError, ValueError):
        return False


def allows(category_settings: dict[str, Any] | None, key: str | None, count: Any) -> bool:
    """Whether a notice of `count` items under `key` is made. An explicit choice of the member wins;
    otherwise the default, which a bulk change overrides to on."""
    if key is None:
        return True
    settings = category_settings or {}
    if key in settings:
        return bool(settings[key])
    return DEFAULTS.get(key, True) or is_bulk(count)

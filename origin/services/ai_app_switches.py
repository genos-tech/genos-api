"""The switches for what a member's AI apps did over MCP (MIP-1432, MIP-1452).

One switch per action: what happened (the notice's `verb`) to what (its `kind`). Each is a key in
`NotificationPreference.category_settings`, under the group master `enable_ai_apps`. The same keys
decide whether the notice is MADE (`mcp_notice`), whether it is pushed (`webpush_gating`) and what
the push is filed under (`webpush_dispatch`), so the client's registry
(`categories.ts`) and this table must agree.

All are on unless the member turned them off, except `ai_app_note_edited`: an AI app can edit one
note dozens of times in a row and each call is its own notice, so that one starts off and the
member opts in.
"""

from __future__ import annotations

from typing import Any

# (verb, kind) -> the switch. A `comment` kind is a comment on a task.
ACTIONS: dict[tuple[str, str], str] = {
    ("created", "task"): "ai_app_task_created",
    ("status", "task"): "ai_app_task_status",
    ("commented", "comment"): "ai_app_task_commented",
    ("deleted", "comment"): "ai_app_comment_deleted",
    ("created", "milestone"): "ai_app_milestone_created",
    ("status", "milestone"): "ai_app_milestone_status",
    ("deleted", "milestone"): "ai_app_milestone_deleted",
    ("created", "note"): "ai_app_note_created",
    ("edited", "note"): "ai_app_note_edited",
    ("deleted", "note"): "ai_app_note_deleted",
    ("created", "todo"): "ai_app_todo_created",
    ("deleted", "todo"): "ai_app_todo_deleted",
}

KEYS: tuple[str, ...] = tuple(ACTIONS.values())

OFF_BY_DEFAULT = frozenset({"ai_app_note_edited"})

DEFAULTS: dict[str, bool] = {key: key not in OFF_BY_DEFAULT for key in KEYS}


def key_for(verb: Any, kind: Any) -> str | None:
    """The switch for an action, or None for one no rule produces (only the group master then
    decides)."""
    return ACTIONS.get((str(verb or ""), str(kind or "")))


def is_on(category_settings: dict[str, Any] | None, key: str | None) -> bool:
    if key is None:
        return True
    return bool((category_settings or {}).get(key, DEFAULTS.get(key, True)))

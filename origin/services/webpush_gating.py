"""Server-side gate for whether to send a Web Push for a given category.

Mirrors the frontend `NotificationManager.isCategoryEnabled` shape
(master && coarse-group && per-category override) but with TWO push-
specific differences:
  - `push_enabled` is an independent master from `master_enabled` (a user
    can keep in-app notifications while turning OFF away-from-app push).
  - Push has its OWN defaults, NOT the in-app `categories.ts` defaults —
    an OS push per message in every group chat is noise. (Slice scope:
    only the mention categories are wired; the full taxonomy + the
    `muted_chats`/`muted_targets` parity land in a later phase.)
"""

from origin.models.common.notification_models import NotificationPreference

# Push-specific defaults when the user has no explicit override. Every
# activity-feed category defaults ON (the product intent is "all
# activities web-notify"); presence + the per-category opt-out below are
# what keep it from being noise. `reactions` has no coarse-group column
# yet (no `enable_reactions` on NotificationPreference), so it is
# fine-category-only — still default ON.
_PUSH_DEFAULTS = {
    "mention_chat": True,
    "mention_thread": True,
    "mention_task": True,
    "mention_note": True,
    "thread_replies": True,
    "task_comments": True,
    "reactions": True,
    "inbox": True,
    # Plain (non-mention) messages in any chat. Default ON per the product
    # decision to notify on all messages; presence + per-chat mute
    # (`muted_chats`) + the `enable_chats` coarse toggle keep it in check.
    "chats": True,
    # A Spotlight / thread / note agent run that finished after the user
    # closed its window. Fired from the run-close path in
    # `agent_views._stream_ndjson`. Low volume by construction — one per
    # backgrounded ask, and only for runs slow enough to be worth it.
    "agent_run_done": True,
}

# Fine category -> the coarse-group boolean column that hard-gates it
# (back-compat with an older client that flipped the whole group off).
# Surface mentions (task body / notes) are still "mentions" and ride the
# same `enable_mentions` coarse toggle. `reactions` is intentionally
# absent — no coarse column exists, so it falls through to the
# fine-category / default check only.
_COARSE_FIELD = {
    "mention_chat": "enable_mentions",
    "mention_thread": "enable_mentions",
    "mention_task": "enable_mentions",
    "mention_note": "enable_mentions",
    "thread_replies": "enable_thread_replies",
    "task_comments": "enable_task_comments",
    "inbox": "enable_inbox",
    "chats": "enable_chats",
    # Rides the `inbox` coarse column (mirrors the frontend registry in
    # `categories.ts`, where `agent_run_done` is grouped under `inbox`).
    # Both are "a system notice addressed to just this user", and reusing
    # the column avoids a migration for a sixth master switch.
    "agent_run_done": "enable_inbox",
}


def should_push(user_id, category: str) -> bool:
    """True when a Web Push for `category` should be sent to `user_id`."""
    prefs = NotificationPreference.objects.filter(user_id=user_id).first()
    if prefs is None:
        # No row yet => every toggle is at its default; push master on.
        return _PUSH_DEFAULTS.get(category, True)
    if not prefs.push_enabled or not prefs.master_enabled:
        return False
    coarse_field = _COARSE_FIELD.get(category)
    if coarse_field and not getattr(prefs, coarse_field):
        return False
    return prefs.category_settings.get(category, _PUSH_DEFAULTS.get(category, True))


def is_chat_muted(user_id, channel_id) -> bool:
    """True when the user has muted the given channel (so no plain-message
    push fires for it). `muted_chats` entries are
    `{"chat_type": int, "chat_id": str}`; the v3 channel UUID is globally
    unique, so matching on `chat_id` alone is sufficient."""
    prefs = NotificationPreference.objects.filter(user_id=user_id).first()
    if prefs is None:
        return False
    cid = str(channel_id)
    for entry in prefs.muted_chats or []:
        if isinstance(entry, dict) and str(entry.get("chat_id")) == cid:
            return True
    return False

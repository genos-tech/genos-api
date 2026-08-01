"""Server-side gate for whether to EMAIL a user about a given category.

Sibling of `webpush_gating` with the same layering (independent channel
master && `master_enabled` && coarse-group && per-category override) but
two email-specific differences:

  - The channel master is `email_enabled`, not `push_enabled`.
  - The defaults are deliberately NOT push's defaults. Push defaults
    everything ON because presence suppression keeps it from being noise;
    email has no equivalent brake once it leaves, so only the "you were
    needed" categories (mentions, assignments, replies on your threads,
    inbox requests) default ON, and firehose categories (`chats`,
    `reactions`) plus in-app-only notices (`agent_run_done`) default OFF.

Per-category overrides live in the SAME `category_settings` JSON as the
in-app/push overrides, under `email:`-prefixed keys
("email:mention_chat"). The unprefixed key must NEVER be consulted as a
fallback: email's defaults differ from the other channels', so inheriting
an unprefixed value would silently flip categories the user only meant to
change in-app.

Known gap inherited from push (see `webpush_gating`'s docstring):
`muted_targets` is not consulted by either channel yet — a muted thread
still emails. `muted_chats` IS honored, by the enqueue path for the
`chats` category (mirroring where push checks it).
"""

from origin.models.common.notification_models import NotificationPreference
from origin.services.webpush_gating import _COARSE_FIELD

# Prefix for email-channel keys inside `category_settings`.
EMAIL_CATEGORY_PREFIX = "email:"

# Email-specific defaults when the user has no explicit `email:` override.
# `task_assign` is email-only for now: `_push_spec` deliberately skips
# ActivityType.TASK_ASSIGN (assignment push rides the mention fan-out),
# but an email digest of "you were assigned X" is exactly what an away
# user needs. Like `reactions`, it has no coarse-group column — it is
# fine-category-only.
_EMAIL_DEFAULTS = {
    "mention_chat": True,
    "mention_thread": True,
    "mention_task": True,
    "mention_note": True,
    "task_assign": True,
    "thread_replies": True,
    "task_comments": True,
    "inbox": True,
    "reactions": False,
    "chats": False,
    "agent_run_done": False,
}


def should_email(user_id, category: str) -> bool:
    """True when a notification email for `category` may go to `user_id`.

    Called twice per event — at enqueue (so default-off categories never
    write outbox rows) and again at send time (prefs may have changed in
    between). Unknown categories fail CLOSED, unlike push's fail-open
    default: mail leaves the product, so an unclassified event must never
    generate any.
    """
    default = _EMAIL_DEFAULTS.get(category, False)
    prefs = NotificationPreference.objects.filter(user_id=user_id).first()
    if prefs is None:
        # No row yet => every toggle is at its default; email master on.
        return default
    if not prefs.email_enabled or not prefs.master_enabled:
        return False
    coarse_field = _COARSE_FIELD.get(category)
    if coarse_field and not getattr(prefs, coarse_field):
        return False
    return prefs.category_settings.get(EMAIL_CATEGORY_PREFIX + category, default)

"""What counts as a usable reminder time, for every kind of reminder.

Shared by `message_reminders` and `todo_reminders` because the client has
only ONE notion of an acceptable instant (`isUsableReminderTime` in
`features/chat/utils/reminderPresets.ts`) and offers the same picker for
both. If the two servers-side windows drifted, one of those pickers would
be handing out times the API answers with a 400 — a failure the UI has no
way to predict, since it computed the instant itself.

Also home to the dead-cron canary (`warn_if_overdue`): the same silence is
possible for either kind, and for the same reason — the nudge, the inbox
card and the push are all the tick's work, so nothing at all is logged
when the tick is not running.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Furthest ahead a reminder may be set. Generous for "remind me about
# this" while keeping the due index from accumulating rows nobody will
# live to see. Mirrored client-side as `MAX_REMINDER_DAYS`.
MAX_HORIZON = timedelta(days=365)

# How far in the past an incoming `remind_at` may be. A client clock a few
# seconds behind the server's shouldn't turn "in 20 minutes" into a 400,
# and anything inside the window fires on the next tick anyway.
PAST_TOLERANCE = timedelta(minutes=2)

# How far past its time a still-pending reminder has to be before it means
# something is wrong rather than "the tick is a few seconds behind".
# Matches `message_reminder_tick.LATE_MINUTES`.
OVERDUE_WARN_AFTER = timedelta(minutes=10)


class ReminderWindowError(ValueError):
    """`remind_at` is in the past or beyond `MAX_HORIZON`."""


def validate_remind_at(remind_at, *, now=None):
    """Return `remind_at` if it is a usable future instant, else raise.

    Kept out of the views so the ticks' own tests can reuse the bounds.
    """
    now = now or timezone.now()
    if remind_at is None:
        raise ReminderWindowError("remindAt is required.")
    if remind_at < now - PAST_TOLERANCE:
        raise ReminderWindowError("remindAt must be in the future.")
    if remind_at > now + MAX_HORIZON:
        raise ReminderWindowError("remindAt must be within a year.")
    return remind_at


def warn_if_overdue(reminders, *, label="reminders", now=None, log=None) -> int:
    """Log a warning when reminders are piling up past their time. Returns
    how late the oldest one is, in seconds.

    `log` is the CALLER's logger, and passing it is not cosmetic: this is a
    canary, so it has to appear under the name of the subsystem that has
    gone quiet (`origin.services.todo_reminders` and friends). Logging every
    kind under this shared module's name instead would hide the warning from
    any log filter or alert scoped to one of them.

    The tick reports its own lateness — but a tick that is not running
    reports nothing, and neither does anything else: the reminder simply
    never arrives, and the inbox stays empty because the inbox item is the
    tick's work too. That silence is how a missing `reminder-tick` service
    survived a production release. The client's boot fetch reads exactly the
    rows that would be overdue, so it costs nothing to notice here.

    `reminders` must be ordered by `remind_at` (as the services'
    `pending_for_user` return them), oldest first.
    """
    if not reminders:
        return 0
    now = now or timezone.now()
    late = int((now - reminders[0].remind_at).total_seconds())
    if late > OVERDUE_WARN_AFTER.total_seconds():
        (log or logger).warning(
            "[%s] %s pending reminder(s), oldest %s minutes past its time — "
            "is the message_reminder_tick cron running?",
            label,
            len(reminders),
            late // 60,
        )
    return late

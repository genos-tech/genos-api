"""Is a user's notifications currently paused (Slack-style DND / snooze)?

The single server-side predicate behind the "pause notifications" feature.
It is consulted by BOTH delivery gates — `webpush_gating.should_push` and
`email_gating.should_email` — so a paused user gets neither an OS push nor a
notification email, matching the in-app toast suppression the frontend does
with the same logic (`src/services/notifications/snooze.ts`).

Two independent forms of snooze live on `NotificationPreference`; either one
active means paused:

  * `snooze_until` — a one-shot "pause until this instant", an absolute UTC
    datetime. Timezone-independent: `now < snooze_until`.
  * `snooze_schedule` — a recurring daily quiet window in the user's LOCAL
    wall-clock, `{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`.

WHY A LOCAL `resolve_snooze_zone` INSTEAD OF `user_time.user_zone`:
`user_time` resolves a user's zone from `.timezone` ALONE. But a user who
picked a location in their profile expects that to win — the frontend and the
profile card both resolve local time as `current_location or timezone`
(see the `current_location` model comment). Reusing `user_time` here would
fire the recurring schedule at the wrong hours for every manual-location user,
disagreeing with the client that set it. So this module has its own resolver
mirroring the client order. `user_time` is left untouched — its other callers
(date-boundary math) depend on its current behavior.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from origin.services.user_time import resolve_zone

logger = logging.getLogger(__name__)


def resolve_snooze_zone(user) -> ZoneInfo:
    """The zone to interpret a user's recurring snooze schedule in.

    `current_location or timezone`, falling back to `settings.TIME_ZONE` when
    neither is set — the same precedence the frontend `resolveZone` and the
    profile card use, and deliberately NOT `user_time.user_zone` (which reads
    only `.timezone`). Accepts `None` for anonymous/system paths.
    """
    name = getattr(user, "current_location", None) or getattr(user, "timezone", None)
    return resolve_zone(name)


def _minutes_of_day(hhmm: str) -> int | None:
    """Parse "HH:MM" (00:00–23:59) into minutes since midnight, or None.

    Malformed input returns None so a bad stored value degrades to
    "not snoozed" rather than raising inside a delivery gate.
    """
    if not isinstance(hhmm, str):
        return None
    parts = hhmm.split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _schedule_active(schedule, now_utc: datetime, zone: ZoneInfo) -> bool:
    """Is the recurring daily window active at `now_utc`?

    `start == end` is treated as "never" (a zero-width window), not "always" —
    the safe reading for an accidental equal pair. `start > end` spans
    midnight (e.g. 17:00–09:00 pauses evenings through the next morning).
    """
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return False
    start = _minutes_of_day(schedule.get("start"))
    end = _minutes_of_day(schedule.get("end"))
    if start is None or end is None or start == end:
        return False

    local = now_utc.astimezone(zone)
    current = local.hour * 60 + local.minute
    if start < end:
        return start <= current < end
    # Overnight window: active from `start` to midnight, then midnight to `end`.
    return current >= start or current < end


def is_snoozed(prefs, now: datetime | None = None) -> bool:
    """True when `prefs` describes a user whose notifications are paused now.

    `prefs` is a `NotificationPreference`. `now` defaults to `timezone.now()`
    (tz-aware UTC); pass an explicit instant in tests. Never raises on a
    malformed `snooze_schedule` — it degrades to "not snoozed" so a bad stored
    value can never wedge a delivery gate.

    The recurring-schedule branch touches `prefs.user` (for the zone); the
    one-shot branch and the no-snooze common case do not, so callers that only
    hold prefs pay the user lookup only when a schedule is actually enabled.
    """
    if prefs is None:
        return False
    if now is None:
        now = timezone.now()

    snooze_until = getattr(prefs, "snooze_until", None)
    if snooze_until is not None and now < snooze_until:
        return True

    schedule = getattr(prefs, "snooze_schedule", None)
    if isinstance(schedule, dict) and schedule.get("enabled"):
        try:
            zone = resolve_snooze_zone(prefs.user)
            return _schedule_active(schedule, now, zone)
        except Exception:  # pragma: no cover - defensive; never wedge a gate
            logger.warning("[notification_snooze] schedule eval failed", exc_info=True)
            return False

    return False

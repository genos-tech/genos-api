"""Turn "remind me …" as an agent or an MCP client says it into one instant.

The REST endpoint takes an absolute instant that the BROWSER computed: it
knows the user's clock and zone, so "tomorrow 9am" is resolved before the
server sees it. An agent has neither. The model is told today's DATE and
never the time (by design — the clock in a prompt busts its cache), and an
MCP client may or may not know the user's zone. So the tools take two
spellings a model can produce reliably and this module resolves them on the
server, where the clock and the user's stored zone are:

* `remind_at` — a local date-time (`2026-11-06T09:00`), read in the user's
  zone; or a bare date (`2026-11-06`), which means `DEFAULT_REMINDER_HOUR`
  that day; or a full ISO instant that carries its own offset (`…+09:00`,
  `…Z`), which is taken as given — a client that does know the real zone
  should be able to say so.
* `remind_in_minutes` — counted from the server clock at the moment the
  call runs. In the app that is after the user approves the card, not when
  the model proposed it.

Both end in the same window the REST endpoint applies
(`reminder_window.validate_remind_at`: not beyond a year), plus one rule the
endpoint can't have: a time that has already passed is refused. The browser's
clock is a few seconds off the server's, which is what the endpoint's
two-minute tolerance is for; the server's own clock has no such excuse, and
"remind me at 9:00" said at 9:30 should be a question, not a notification
that fires at once.

Errors carry the current time in the user's zone, because the model that got
it wrong has no other way to find out what time it is.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import dateparse, timezone

from origin.services.reminder_window import MAX_HORIZON, ReminderWindowError, validate_remind_at

# "Remind me on Friday" names no hour. Morning is what the app's own
# "tomorrow" preset means, and the least surprising time to be nudged about
# a day's work.
DEFAULT_REMINDER_HOUR = 9

MAX_MINUTES = int(MAX_HORIZON.total_seconds() // 60)


class ReminderInputError(ValueError):
    """The reminder the caller asked for cannot be honoured; the message says
    why, in terms of the tool's own arguments."""


def _now_text(now: datetime, zone: ZoneInfo) -> str:
    return f"It is now {now.astimezone(zone).strftime('%Y-%m-%d %H:%M')} ({zone.key})."


def _minutes(raw) -> int:
    # bool is an int subclass; `true` is not a number of minutes.
    if isinstance(raw, bool):
        raise ReminderInputError("`remind_in_minutes` must be a whole number of minutes.")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ReminderInputError(f"`remind_in_minutes` must be a number (got {raw!r}).") from None
    if number != number or number in (float("inf"), float("-inf")) or number != int(number):
        raise ReminderInputError("`remind_in_minutes` must be a whole number of minutes.")
    minutes = int(number)
    if minutes < 1:
        raise ReminderInputError("`remind_in_minutes` must be at least 1.")
    if minutes > MAX_MINUTES:
        raise ReminderInputError(
            f"`remind_in_minutes` must be at most {MAX_MINUTES} (a year); "
            "use `remind_at` for a date that far out."
        )
    return minutes


def _local_instant(raw, zone: ZoneInfo) -> datetime:
    text = str(raw).strip()
    # A bare date first: Python 3.11's `fromisoformat` reads "2026-11-06" as
    # midnight, which would quietly turn "remind me Friday" into 00:00.
    try:
        day = dateparse.parse_date(text)
    except ValueError:
        day = None
    if isinstance(day, date):
        return datetime.combine(day, time(DEFAULT_REMINDER_HOUR), tzinfo=zone)
    try:
        parsed = dateparse.parse_datetime(text)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ReminderInputError(
            f"`remind_at` must be an ISO date or date-time like 2026-11-06T09:00 (got {raw!r})."
        )
    return parsed if timezone.is_aware(parsed) else parsed.replace(tzinfo=zone)


def resolve_remind_at(
    *, remind_at=None, remind_in_minutes=None, zone: ZoneInfo, now: datetime | None = None
) -> datetime | None:
    """The absolute instant the caller asked for, or None when they asked for
    no reminder. Raises `ReminderInputError` for anything that can't be one.

    `zone` is the asking user's (`user_time.zone_for_user_id`) and is only
    consulted for a time that carries no offset of its own.
    """
    given_at = remind_at not in (None, "")
    given_in = remind_in_minutes is not None
    if given_at and given_in:
        raise ReminderInputError("Give `remind_at` or `remind_in_minutes`, not both.")
    if not given_at and not given_in:
        return None

    now = now or timezone.now()
    if given_in:
        instant = now + timedelta(minutes=_minutes(remind_in_minutes))
    else:
        instant = _local_instant(remind_at, zone)
        if instant <= now:
            raise ReminderInputError(
                f"`remind_at` ({remind_at}) has already passed. {_now_text(now, zone)}"
            )
    try:
        return validate_remind_at(instant, now=now)
    except ReminderWindowError as exc:
        # The window is the REST endpoint's; it names that endpoint's field.
        name = "remind_in_minutes" if given_in else "remind_at"
        raise ReminderInputError(str(exc).replace("remindAt", f"`{name}`")) from None

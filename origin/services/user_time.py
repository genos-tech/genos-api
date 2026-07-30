"""Per-user date-boundary math.

`settings.TIME_ZONE` is UTC and stays UTC — that is correct for *storage*.
What was wrong is that every "today" in the codebase was also UTC, so for a
user in Tokyo (UTC+9) the server's idea of today was a day behind for nine
hours out of every twenty-four. Ask Spotlight "what's due today" at 9am JST
and it answered for yesterday.

This module is the one place that decides which calendar day a moment falls
in for a given user. Call `user_today(user)` instead of
`timezone.now().date()` / `timezone.localdate()` / `date.today()` anywhere the
answer is scoped to one person.

WHEN NOT TO USE IT — the distinction is whose day it is:

  * **Personal scope** — "my tasks due today", "my todo groups", a digest
    addressed to one person. The viewer's day. Use this module.
  * **Shared scope** — a sprint's day count, a team velocity bucket. Two
    users in different zones must not get different bytes for the same team
    resource, and if the response is cached under a user-agnostic key the
    second reader gets the first reader's day. Leave those on server time.
  * **Quota / billing windows** (`search_engine/quota.py`) — MUST stay on a
    fixed zone. A per-user daily window is resettable by changing your
    timezone, i.e. free quota on demand.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def is_valid_timezone(name: str | None) -> bool:
    """Is `name` an IANA zone this Python/tzdata knows about?

    `available_timezones()` builds a set of ~600 names, so the result is
    cached by the caller pattern below rather than called per row.
    """
    if not name or not isinstance(name, str):
        return False
    return name in available_timezones()


def resolve_zone(name: str | None) -> ZoneInfo:
    """A `ZoneInfo` for `name`, falling back to `settings.TIME_ZONE`.

    Never raises. An unknown or missing name is normal — most rows are NULL
    until the client reports one — so this is a fallback, not an error path.
    """
    if is_valid_timezone(name):
        try:
            return ZoneInfo(name)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - defensive; validated above
            logger.warning("[user_time] ZoneInfo(%r) failed; falling back", name)
    return ZoneInfo(settings.TIME_ZONE)


def user_zone(user) -> ZoneInfo:
    """The zone to interpret dates in for `user`.

    Accepts anything with a `.timezone` attribute, plus `None` (anonymous /
    system paths), so callers don't need their own guard.
    """
    return resolve_zone(getattr(user, "timezone", None))


def user_now(user) -> datetime:
    """`timezone.now()` expressed in the user's zone.

    Same instant, different wall clock — use this when you need the user's
    hour, not just their date.
    """
    return timezone.now().astimezone(user_zone(user))


def user_today(user) -> date:
    """The calendar date it currently is *for this user*.

    The replacement for `timezone.now().date()` in personal-scope code. With
    a NULL `user.timezone` this is byte-identical to the old behaviour, which
    is what makes adopting it safe one call site at a time.
    """
    return user_now(user).date()

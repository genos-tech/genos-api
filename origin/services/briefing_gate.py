"""One `get_my_briefing` in flight per member (MIP-2092, part 2).

Four AI sessions of one member asked for a briefing in the same second and the service (one
gunicorn worker x 8 threads, 0.5 CPU) ran all four at once: ~4 CPU seconds each, 45 KB answers
built in memory, 8 request threads gone. Memory spiked, the worker was killed and every request in
flight answered 502. A briefing is the member's own view, so a second one asked while the first is
being built is mostly wasted work: the member's session can retry a few seconds later, when the
first has finished.

What the second call gets is a GUIDE, not a refusal: an ordinary result (`isError: false`) with a
sentence that says a briefing is already being built and when to ask again, and
`_meta["genos/briefing"] = {"in_flight": true, "retry_after": N}` for a client that would rather
branch. Nothing is queued and nothing waits (a request thread held for 10 s is what hurt).

The lock is a short-lived key in the shared cache (`mcp:briefing:inflight:<team>:<user>`), taken
with `add` (atomic in Redis) and given a token, so a call only ever releases its own lock. The TTL
is a bound, not a promise: a worker that is killed mid-briefing never releases, so the key expires
by itself (`BRIEFING_LOCK_SECONDS`, 30 by default; the slowest briefing measured in production was
15 s). If the cache cannot be reached the call is ALLOWED (as every other limit here does): the lock
is a convenience, never a reason for a member not to get their briefing.

Keys carry ids only, never content.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from origin.services.throttles import CACHE_ERRORS, throttle_cache

log = logging.getLogger(__name__)

TOOL_NAME = "get_my_briefing"


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


# How long a lock lives if its call never releases it (worker killed, thread hung).
LOCK_SECONDS = _env_int("BRIEFING_LOCK_SECONDS", 30)
# What the second call is told to wait: about as long as a briefing takes (7 to 15 s in production
# before the zone-set fix; 3 s is enough on a board that fits in cache), never more than the lock.
RETRY_AFTER_SECONDS = min(_env_int("BRIEFING_RETRY_AFTER_SECONDS", 10), LOCK_SECONDS)

# Switch: 0 turns the gate off (every call runs, as before this change).
_ENABLED = os.environ.get("BRIEFING_ONE_IN_FLIGHT", "1").strip() != "0"


def _key(team_id: Any, user_id: Any) -> str:
    return f"mcp:briefing:inflight:{team_id}:{user_id}"


class Held:
    """A lock this call owns. Release it in a `finally`."""

    __slots__ = ("key", "token")

    def __init__(self, key: str, token: str) -> None:
        self.key = key
        self.token = token


class Busy:
    """Another briefing of the same member is being built."""

    __slots__ = ("retry_after",)

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


def enter(team_id: Any, user_id: Any) -> Held | Busy | None:
    """Take the member's briefing lock. Returns `Held` (release it when done), `Busy` (answer
    with `busy_result`), or None (the gate is off or the cache is down: just run the call)."""
    if not _ENABLED or user_id is None:
        return None
    key = _key(team_id, user_id)
    token = uuid.uuid4().hex
    try:
        if throttle_cache().add(key, token, LOCK_SECONDS):
            return Held(key, token)
    except CACHE_ERRORS as exc:
        log.warning(
            "mcp.briefing_gate store unavailable; allowing the call (%s)", type(exc).__name__
        )
        return None
    log.info("mcp.briefing_gate busy team=%s user=%s", team_id, user_id)
    return Busy(RETRY_AFTER_SECONDS)


def leave(held: Held | None) -> None:
    """Release the lock if it is still this call's own. Never raises."""
    if held is None:
        return
    try:
        cache = throttle_cache()
        if cache.get(held.key) == held.token:
            cache.delete(held.key)
    except CACHE_ERRORS as exc:
        # It expires by itself after `LOCK_SECONDS`.
        log.warning("mcp.briefing_gate release failed (%s)", type(exc).__name__)


def busy_result(busy: Busy) -> dict[str, Any]:
    """The answer for the second call: a suggestion for the model, not an error."""
    seconds = busy.retry_after
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "A briefing for you is already being built (another call of yours asked for "
                    f"it a moment ago), so no new one was started. Retry `{TOOL_NAME}` in about "
                    f"{seconds} seconds; until then, carry on with other work or tell the member "
                    "the briefing is on its way."
                ),
            }
        ],
        "isError": False,
        "_meta": {"genos/briefing": {"in_flight": True, "retry_after": seconds}},
    }

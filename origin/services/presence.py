"""Lightweight "is this user actively looking at a visible tab?" presence.

Used to avoid sending a Web Push to someone who has the app open and
focused — they get the in-app toast instead, so a push would be a
duplicate OS notification.

Fed by a heartbeat the frontend sends ONLY while
`document.visibilityState === "visible"` (POST /api/v2/user/presence/
heartbeat); read by the push dispatcher. Backed by the shared Redis cache
with a short TTL so it self-heals: when the user hides/closes the tab the
heartbeat stops and the key expires (~PRESENCE_TTL_SECONDS), after which
pushes resume.

Cache semantics are acceptable: a lost key just means a push that could
have been suppressed gets sent anyway (the SW still shows exactly one
notification — no duplication). `DJANGO_REDIS_IGNORE_EXCEPTIONS=True`
makes a Redis outage fail-open (treated as not-visible → push sent),
which is the safe direction.

NOTE (productionization): this adds a small HTTP heartbeat distinct from
the existing socket `presence.ping`. A later iteration can fold this into
the socket layer (Flask writing the same Redis key on ping) to drop the
extra request.
"""

from django.core.cache import cache

# A hidden/closed tab's heartbeat stops; the key expires after this, after
# which the user is considered "away" and eligible for push. Slightly above
# the ~45s client heartbeat interval to tolerate a missed beat.
PRESENCE_TTL_SECONDS = 90


def _key(user_id) -> str:
    return f"presence:visible:{user_id}"


def mark_visible(user_id) -> None:
    """Record that the user has a visible tab (call on each heartbeat)."""
    cache.set(_key(user_id), "1", timeout=PRESENCE_TTL_SECONDS)


def has_visible_tab(user_id) -> bool:
    """True when the user has reported a visible tab within the TTL."""
    return cache.get(_key(user_id)) is not None

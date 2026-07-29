"""Lightweight "is this DEVICE actively looking at a visible tab?" presence.

Used to avoid sending a Web Push to a device whose app is open and
focused — it shows the in-app toast instead, so a push would be a
duplicate OS notification.

PER-DEVICE, deliberately. This was originally keyed per user, which meant
one foreground tab anywhere suppressed push to every device that user
owned: leave Genos open on your laptop and your phone goes silent
indefinitely, which is the opposite of what a phone is for. The heartbeat
and the push subscription now both carry a `device_id`, so suppression
applies only to the device actually being looked at.

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


def _device_key(user_id, device_id) -> str:
    return f"presence:visible:{user_id}:{device_id}"


def mark_visible(user_id, device_id: str = "") -> None:
    """Record that the user has a visible tab (call on each heartbeat).

    Writes the per-device key when the client sent a `device_id`, and
    always writes the per-user key: subscriptions registered before
    per-device presence have no device of their own, and that key is what
    still suppresses them.
    """
    cache.set(_key(user_id), "1", timeout=PRESENCE_TTL_SECONDS)
    if device_id:
        cache.set(_device_key(user_id, device_id), "1", timeout=PRESENCE_TTL_SECONDS)


def clear_visible(user_id, device_id: str = "") -> None:
    """Drop this device's presence immediately (tab hidden / app closed).

    Without this the TTL alone decides, which leaves a real hole on iOS:
    a backgrounded PWA has its JavaScript suspended after ~30s, so the
    page stops raising its own notifications — but the presence key lives
    for 90s, so the server keeps suppressing push. Between those two the
    user gets NOTHING. An explicit clear on `visibilitychange` closes it.

    The per-user key is only cleared when no device id is supplied: with
    one, other devices may still be visible and own that key.
    """
    if device_id:
        cache.delete(_device_key(user_id, device_id))
    else:
        cache.delete(_key(user_id))


def has_visible_tab(user_id) -> bool:
    """True when the user has ANY visible tab within the TTL.

    Kept for the legacy fallback below — prefer `is_device_visible`.
    """
    return cache.get(_key(user_id)) is not None


def is_device_visible(user_id, device_id: str) -> bool:
    """True when THIS device reported a visible tab within the TTL.

    A subscription with no `device_id` predates per-device presence, so it
    falls back to the any-tab check rather than being pushed while the
    user is demonstrably looking at something — those rows heal as soon as
    the client re-registers, which it does on every app load.
    """
    if not device_id:
        return has_visible_tab(user_id)
    return cache.get(_device_key(user_id, device_id)) is not None

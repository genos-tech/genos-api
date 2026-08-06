"""Lightweight "is this DEVICE actively looking at a visible tab?" presence,
plus "and WHICH conversation is on screen" (see `mark_viewing`).

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


# ── which conversation is on screen ────────────────────────────────────
#
# One step finer than `mark_visible`: not just "a tab is visible" but
# "THIS conversation is the one being looked at". Used by
# `services/v3_activity` to skip the sidebar activity row for a recipient
# who is demonstrably already reading the thing that would have produced
# it — two people chatting in an open DM don't need a feed entry per
# message telling them about the conversation they are having.
#
# A surface is an opaque token minted by the frontend from the same state
# that drives its in-app toast suppression (`notificationManager`'s
# active surface), so the two rules can't drift:
#
#     channel:<channel_uuid>            main timeline of a channel
#     thread:<thread_root_message_id>   a thread pane
#     task:<task_id>                    a task preview (its comments)
#
# Keyed by (user, surface) rather than (user, device) so the read side is
# a single O(1) `cache.get` with no device enumeration, and so two devices
# open on the same conversation simply both assert the same key.
#
# The failure direction is the opposite of `mark_visible`'s and it is why
# this is deliberately conservative. A lost key there means a suppressible
# push is sent (harmless duplicate); a WRONG key here means an activity
# row is never written, and the recipient loses it for good. So: the TTL
# is short relative to how long people sit in one conversation, the
# heartbeat re-asserts it, and a device that moves away clears the surface
# it left (`_viewing_device_key` remembers which one that was) instead of
# waiting out the TTL. A Redis outage reads as "not viewing" → the row is
# written, which is the safe direction.
VIEWING_TTL_SECONDS = 90

# Surfaces are client-supplied and become part of a cache key, so anything
# outside the token grammar above is refused: no unbounded keys, and no
# control characters or spaces (which Django's memcached key validator
# rejects outright and which would make the key unreadable in Redis).
_MAX_SURFACE_LEN = 120
_SURFACE_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-_")


def clean_surface(surface) -> str:
    """Validate a client-supplied surface token, or `""` if unusable.

    Refuses rather than truncates: a token cut to the length cap would
    still be *a* valid key, so two different long surfaces could collide
    onto it and suppress each other's activities.
    """
    if not surface:
        return ""
    text = str(surface)
    if len(text) > _MAX_SURFACE_LEN or not set(text) <= _SURFACE_ALLOWED:
        return ""
    return text


def _viewing_key(user_id, surface) -> str:
    return f"presence:viewing:{user_id}:{surface}"


def _viewing_device_key(user_id, device_id) -> str:
    """Remembers the surface a device last asserted, so moving to another
    conversation (or closing the tab) can retract the old one immediately
    rather than leaving it suppressing activities for the rest of the TTL."""
    return f"presence:viewing-at:{user_id}:{device_id}"


def mark_viewing(user_id, surface: str, device_id: str = "") -> None:
    """Record that the user is looking at `surface` (call on each heartbeat).

    An empty surface means "in the app but not in any conversation", which
    retracts whatever this device claimed before.
    """
    surface = clean_surface(surface)
    if not surface:
        clear_viewing(user_id, device_id)
        return
    if device_id:
        previous = cache.get(_viewing_device_key(user_id, device_id))
        if previous and previous != surface:
            cache.delete(_viewing_key(user_id, previous))
        cache.set(_viewing_device_key(user_id, device_id), surface, timeout=VIEWING_TTL_SECONDS)
    cache.set(_viewing_key(user_id, surface), "1", timeout=VIEWING_TTL_SECONDS)


def clear_viewing(user_id, device_id: str = "") -> None:
    """Retract this device's claim (navigated away / tab hidden / closed).

    Needs the `device_id` to know WHICH surface to retract; without one
    there is nothing to look up and the TTL is the only backstop. That
    matches the caller: the frontend sends its device id on every beat.
    """
    if not device_id:
        return
    previous = cache.get(_viewing_device_key(user_id, device_id))
    if previous:
        cache.delete(_viewing_key(user_id, previous))
    cache.delete(_viewing_device_key(user_id, device_id))


def is_viewing(user_id, surface: str) -> bool:
    """True when any of this user's devices reported `surface` on screen
    within the TTL."""
    surface = clean_surface(surface)
    if not surface:
        return False
    return cache.get(_viewing_key(user_id, surface)) is not None


def viewers_of(surface: str, user_ids) -> set:
    """Which of `user_ids` are looking at `surface` right now.

    One `get_many` instead of a `cache.get` per candidate: this runs on
    the message-send path, where the candidate set is every member of the
    channel.
    """
    surface = clean_surface(surface)
    ids = [str(uid) for uid in user_ids if uid]
    if not surface or not ids:
        return set()
    keys = {_viewing_key(uid, surface): uid for uid in ids}
    found = cache.get_many(list(keys))
    return {keys[key] for key in found}

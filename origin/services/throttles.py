"""Rate limits, and what a limit does when it can't count (MIP-724).

Three things the stock DRF throttles got wrong here:

**They failed open, invisibly.** The default cache is django-redis with
`IGNORE_EXCEPTIONS`, so with Redis down `cache.get` returned the default —
an empty history — and every request was allowed. For most limits that is
the right trade (a cache outage shouldn't take the whole app down with
it), but for sign-in and password reset it meant guessing was unlimited
for exactly as long as Redis was down. Every throttle here reads the
`throttle` cache alias instead: the same Redis *without*
`IGNORE_EXCEPTIONS`, so the error reaches the throttle, which decides —
`fail_closed = True` (the auth throttles) refuses with a 429, anything
else allows and logs that it did. With no `throttle` alias configured
(the test settings) they read `default`, as before.

**A 429 was silent.** DRF returns it and logs nothing, so a scrape being
held off and a real user being wrongly limited looked the same. Every
denial here logs one `throttle.denied` line and bumps a per-scope,
per-hour counter (`denied_count`, `manage.py throttle_denials`).

**A sliding window costs O(limit).** `SimpleRateThrottle` keeps every
request's timestamp in one cached list and rewrites the whole list on
each request. That is fine at sign-in's 20/min; at a per-user default of
thousands an hour it is ~100 KB to and from Redis on every API call. The
per-user limits (`WindowRateThrottle`) count in fixed windows instead —
one `INCR` per request — at the cost of allowing up to twice the rate
across a window boundary, which for a backstop limit is fine.
"""

from __future__ import annotations

import logging
import time

from django.core.cache import InvalidCacheBackendError, caches
from django.core.exceptions import ImproperlyConfigured
from django_redis.exceptions import ConnectionInterrupted
from rest_framework.settings import api_settings
from rest_framework.throttling import AnonRateThrottle, BaseThrottle

logger = logging.getLogger(__name__)

THROTTLE_CACHE = "throttle"

# What a cache that can't be reached raises: django-redis wraps every
# Redis connection/timeout error in ConnectionInterrupted, and a bare
# socket error is an OSError. `CacheDown` (below) is the breaker.
CACHE_ERRORS: tuple[type[Exception], ...] = (ConnectionInterrupted, OSError)

# Every scope a throttle here has been declared with, for the
# `throttle_denials` command to list.
SCOPES: set[str] = set()

# Denial counts are kept two days, so yesterday is there to compare with.
_COUNT_TTL = 2 * 24 * 3600

# After a cache error, this process stops asking the cache for this long
# and goes straight to the fail-open/closed answer. Every request passes
# two throttles, so without it a Redis that hangs rather than refuses
# would add two connect timeouts to every request in the app.
_BREAKER_SECONDS = 5.0
_down_until = 0.0


class CacheDown(Exception):
    """The breaker is open: don't try the cache."""


CACHE_ERRORS += (CacheDown,)


def throttle_cache(*, breaker: bool = True):
    """The cache throttles count in. `breaker=False` for the fail-closed
    ones: they guard low-traffic endpoints, so they can afford to try
    every time, and one blip elsewhere must not lock everyone out of
    sign-in for the breaker's whole window."""
    if breaker and time.monotonic() < _down_until:
        raise CacheDown
    try:
        return caches[THROTTLE_CACHE]
    except InvalidCacheBackendError:
        return caches["default"]


def _trip_breaker() -> None:
    global _down_until
    _down_until = time.monotonic() + _BREAKER_SECONDS


def _count_key(scope: str, hour: int) -> str:
    return f"throttle_denied:{scope}:{hour}"


def denied_count(scope: str, hour: int | None = None) -> int:
    """429s served for `scope` in epoch-hour `hour` (default: this hour).
    Best effort — read from the default cache, so 0 with Redis down."""
    hour = int(time.time() // 3600) if hour is None else hour
    try:
        return int(caches["default"].get(_count_key(scope, hour)) or 0)
    except Exception:
        return 0


def _record_denial(scope: str, request, who: str) -> None:
    logger.warning(
        "throttle.denied scope=%s %s method=%s path=%s",
        scope,
        who,
        request.method,
        request.path,
    )
    key = _count_key(scope, int(time.time() // 3600))
    try:
        cache = caches["default"]
        if not cache.add(key, 1, _COUNT_TTL):
            cache.incr(key)
    except Exception:
        # The count is a convenience; the line above is the record.
        pass


def _who(throttle, request) -> str:
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"user={user.pk}"
    return f"ip={throttle.get_ident(request)}"


def _cache_down(throttle, request, exc) -> bool:
    if not isinstance(exc, CacheDown):
        _trip_breaker()
    throttle._cache_was_down = True
    if throttle.fail_closed:
        logger.error(
            "throttle.cache_unavailable scope=%s action=refuse %s path=%s",
            throttle.scope,
            _who(throttle, request),
            request.path,
            exc_info=not isinstance(exc, CacheDown),
        )
        return False
    logger.warning(
        "throttle.cache_unavailable scope=%s action=allow path=%s",
        throttle.scope,
        request.path,
        exc_info=not isinstance(exc, CacheDown),
    )
    return True


# ── Sliding window, for the small per-IP limits ───────────────────────────


class RecordedThrottleMixin:
    """For a `SimpleRateThrottle`: read the `throttle` cache, record each
    denial, and on a cache error refuse if `fail_closed` else allow.
    List it before the DRF base."""

    fail_closed = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if getattr(cls, "scope", None):
            SCOPES.add(cls.scope)

    @property
    def cache(self):
        return throttle_cache(breaker=not self.fail_closed)

    def allow_request(self, request, view):
        self._cache_was_down = False
        try:
            allowed = super().allow_request(request, view)
        except CACHE_ERRORS as exc:
            return _cache_down(self, request, exc)
        if not allowed:
            _record_denial(self.scope, request, _who(self, request))
        return allowed

    def wait(self):
        # Nothing was read, so there is no history to compute a wait from.
        if self._cache_was_down:
            return 60
        return super().wait()


class AuthThrottle(RecordedThrottleMixin, AnonRateThrottle):
    """Per-IP limit on a sign-in, sign-up or reset endpoint. A security
    control, so with the cache down it refuses rather than allows: a few
    minutes of "try again shortly" on sign-in beats unlimited guessing."""

    fail_closed = True


# ── Fixed window, for the per-user limits ─────────────────────────────────


_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    """`"600/min"` → `(600, 60)`. Spelled as DRF spells rates: only the
    period's first letter counts."""
    num, period = rate.split("/")
    return int(num), _PERIODS[period.strip()[0]]


class WindowRateThrottle(BaseThrottle):
    """Per-user count in fixed windows; anonymous requests pass (they are
    the per-IP throttles' job). Fail open unless `fail_closed`.

    `rate` on the class wins; otherwise `DEFAULT_THROTTLE_RATES[scope]`,
    so a rate can be tuned in production with a variable, not a deploy.
    `methods`, when set, limits which requests count — an endpoint polled
    with GET while a POST starts the expensive work throttles the POST.
    `cost(request)` is how much of the budget a request uses; override it
    where one request can do a variable amount of the expensive thing. A
    refused request still spends its cost, so a script that keeps trying
    stays refused until the window turns over.
    """

    scope: str = ""
    rate: str | None = None
    methods: frozenset[str] | None = None
    fail_closed = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.scope:
            SCOPES.add(cls.scope)

    def __init__(self):
        rate = self.rate or api_settings.DEFAULT_THROTTLE_RATES.get(self.scope)
        if not rate:
            raise ImproperlyConfigured(f"No throttle rate for scope {self.scope!r}")
        self.num_requests, self.duration = parse_rate(rate)
        self._wait = None
        self._cache_was_down = False

    @property
    def cache(self):
        return throttle_cache(breaker=not self.fail_closed)

    def allow_request(self, request, view):
        if self.methods is not None and request.method not in self.methods:
            return True
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return True
        now = time.time()
        window = int(now // self.duration)
        key = f"throttle:{self.scope}:{user.pk}:{window}"
        self._cache_was_down = False
        try:
            count = self._hit(key, max(1, self.cost(request)))
        except CACHE_ERRORS as exc:
            return _cache_down(self, request, exc)
        if count <= self.num_requests:
            return True
        self._wait = (window + 1) * self.duration - now
        _record_denial(self.scope, request, f"user={user.pk}")
        return False

    def cost(self, request) -> int:
        return 1

    def _hit(self, key, cost: int) -> int:
        cache = self.cache
        # A second past the window so a request at its last instant
        # can't find the key gone before its own INCR lands.
        if cache.add(key, cost, self.duration + 1):
            return cost
        try:
            return cache.incr(key, cost)
        except ValueError:
            # Expired between the add and the incr: this is the first
            # request of what's left of the window.
            cache.set(key, cost, self.duration + 1)
            return cost

    def wait(self):
        if self._cache_was_down:
            return 60
        return self._wait


# ── The default for every view ────────────────────────────────────────────
#
# REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"], so these apply to every view
# that doesn't name its own `throttle_classes`. Generous on purpose: what
# they stop is a script, never a person. One page boot is ~30 calls; a
# sockets reconnect replays missed messages with two calls per channel
# for up to 200 channels, all on the user's own token; the collab server
# re-checks every open note once a minute on it too. The burst limit sits
# well above two reconnects in a minute.


class UserBurstThrottle(WindowRateThrottle):
    scope = "user_burst"


class UserSustainedThrottle(WindowRateThrottle):
    scope = "user_sustained"


DEFAULTS = [UserBurstThrottle, UserSustainedThrottle]


def with_defaults(*extra):
    """`throttle_classes` for a view with its own scope. Naming
    `throttle_classes` replaces the defaults, so this puts them back."""
    return [*extra, *DEFAULTS]


# ── Tighter scopes for the expensive endpoints ────────────────────────────


class SearchThrottle(WindowRateThrottle):
    """An OpenSearch query (and an embedding) per call."""

    scope = "search"


class AgentThrottle(WindowRateThrottle):
    """An LLM call — or a whole agent run — per request. The monthly
    quota caps spend; this caps the rate, which a quota can't see."""

    scope = "agent"
    methods = frozenset({"POST"})


class SummaryThrottle(WindowRateThrottle):
    """The thread/note summary panels. An LLM call on a cache miss, and
    each open panel re-asks every 30 s to spot a changed fingerprint —
    hence its own scope, so a few open panels can't use up `agent`."""

    scope = "agent_summary"


class ExportThrottle(WindowRateThrottle):
    """A full account export: every row the user can see, serialised."""

    scope = "export"


class InviteThrottle(WindowRateThrottle):
    """Each address in an invite is an email sent to someone the caller
    chose, so the budget is in addresses, not requests — 30 requests an
    hour would still have been 30 × however many addresses fit in one."""

    scope = "invite"
    methods = frozenset({"POST"})

    def cost(self, request) -> int:
        emails = request.data.get("emails") if hasattr(request.data, "get") else None
        if not isinstance(emails, list):
            return 1
        return len({str(e).strip().lower() for e in emails if e})


class BatchReadThrottle(WindowRateThrottle):
    """Endpoints that take a list of ids and answer for every one."""

    scope = "batch_read"

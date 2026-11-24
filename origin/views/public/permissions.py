"""Permission and throttle classes for the public API.

The public surface differs from the app surface in three ways, and each
one is a class here rather than an inline check, because this is the
first API in the product that strangers write code against — the rules
have to be legible in one place.

1. **API key only.** A session JWT is refused. The public API is for
   automation; letting a browser session drive it would mean rate
   limits, audit and scope all keyed on something that isn't the
   credential the integrator manages.
2. **Scopes are enforced on the METHOD**, not per endpoint, so a
   read-only key cannot reach any write path even one added later. A new
   endpoint is safe by default rather than safe if remembered.
3. **Rate limits are per key**, so one noisy integration cannot spend
   another's budget.
"""

from __future__ import annotations

import logging
import threading
import time

from rest_framework import throttling
from rest_framework.permissions import SAFE_METHODS, BasePermission

from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey
from origin.services.throttles import CacheDown, RecordedThrottleMixin, _trip_breaker
from origin.views.public.credentials import credential_of


class HasApiKey(BasePermission):
    """The caller authenticated with an API key, not a session."""

    message = "This endpoint requires an API key."

    def has_permission(self, request, view) -> bool:
        return isinstance(getattr(request, "auth", None), ApiKey)


class HasCredential(BasePermission):
    """The caller authenticated with a credential MCP accepts: an API key, or any other
    kind that satisfies `Credential` (MIP-1078). A session is refused, as in `HasApiKey`.

    MCP uses this where REST uses `HasApiKey`: an OAuth token is for MCP only, so it can
    stand here and still never reach a REST view."""

    message = "This endpoint requires an API key or a connected app."

    def has_permission(self, request, view) -> bool:
        return credential_of(request) is not None


class HasRequiredScope(BasePermission):
    """A read-only key may only use safe methods.

    Keyed on the HTTP method rather than a per-view declaration on
    purpose: a view added later inherits the rule instead of having to
    remember it, and "someone forgot to declare a scope" then costs a
    403 rather than an unintended write.
    """

    message = "This API key is read-only."

    def has_permission(self, request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        key = getattr(request, "auth", None)
        return isinstance(key, ApiKey) and key.scope == SCOPE_WRITE


logger = logging.getLogger(__name__)

_local_lock = threading.Lock()
_local_counts: dict[str, tuple[float, int]] = {}
_LOCAL_PRUNE_AT = 5_000


def _local_hit(key: str, window: int) -> int:
    """Count one request in this process for `window` seconds and return the count."""
    now = time.monotonic()
    with _local_lock:
        expires, count = _local_counts.get(key, (0.0, 0))
        if expires <= now:
            expires, count = now + window, 0
        count += 1
        _local_counts[key] = (expires, count)
        if len(_local_counts) > _LOCAL_PRUNE_AT:
            for stale in [k for k, (exp, _n) in _local_counts.items() if exp <= now]:
                del _local_counts[stale]
        return count


class PublicApiThrottle(RecordedThrottleMixin, throttling.SimpleRateThrottle):
    """Per-KEY rate limiting.

    DRF's `UserRateThrottle` would key on the user, so a person's five
    integrations would share one bucket and a runaway script would starve
    the others. The key is the unit an integrator actually controls, so
    it is the unit that gets the budget — and revoking a key frees it.

    **With Redis down it counts in this process** (MIP-2266), against the same rate, instead of
    allowing everything. The count is per worker, so it can only UNDER-count (N workers allow up to
    N times the rate), but a leaked key can no longer read without any ceiling while the store is
    out. A request over the in-process count is refused with a 429, and each fallback is logged.
    """

    scope = "public_api"

    def on_cache_down(self, request, exc) -> bool:
        if not isinstance(exc, CacheDown):
            _trip_breaker()
        self._cache_was_down = True
        key = self.get_cache_key(request, None)
        if key is None:
            return True  # no credential: the permission class rejects it anyway
        count = _local_hit(key, self.duration)
        allowed = count <= self.num_requests
        logger.warning(
            "throttle.cache_unavailable scope=%s action=%s count=%s",
            self.scope,
            "count_in_process" if allowed else "refuse",
            count,
        )
        return allowed

    def get_cache_key(self, request, view):
        credential = credential_of(request)
        if credential is None:
            return None  # Not throttled here; the permission rejects it anyway.
        # An API key's bucket is its id, as it always was; another kind brings its own,
        # so two kinds can never share one.
        return self.cache_format % {"scope": self.scope, "ident": credential.throttle_ident()}


class McpApiThrottle(PublicApiThrottle):
    """The MCP endpoint's per-credential backstop (MIP-1614).

    The same bucket rule as `PublicApiThrottle`, under its own scope and rate (`public_api_mcp`,
    400/min) so the REST API keeps its 120/min while Max's 240 calls a minute
    (`origin/services/mcp_tier_limits.py`) can be reached. The per-plan number is the plan limit;
    this only caps a runaway client, with room for requests that are not counted as calls.
    """

    scope = "public_api_mcp"

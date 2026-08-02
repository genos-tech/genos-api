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

from rest_framework import throttling
from rest_framework.permissions import SAFE_METHODS, BasePermission

from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey


class HasApiKey(BasePermission):
    """The caller authenticated with an API key, not a session."""

    message = "This endpoint requires an API key."

    def has_permission(self, request, view) -> bool:
        return isinstance(getattr(request, "auth", None), ApiKey)


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


class PublicApiThrottle(throttling.SimpleRateThrottle):
    """Per-KEY rate limiting.

    DRF's `UserRateThrottle` would key on the user, so a person's five
    integrations would share one bucket and a runaway script would starve
    the others. The key is the unit an integrator actually controls, so
    it is the unit that gets the budget — and revoking a key frees it.

    ⚠️ **This fails OPEN.** `DJANGO_REDIS_IGNORE_EXCEPTIONS = True`, so a
    Redis outage means *unlimited*, not zero. That is the deliberate
    house policy for quota (`search_engine/quota.py` states it), and it
    is the right trade for the app — but it is worth knowing that the
    public API's rate limit is an availability measure, not a security
    control. Anything that must hold under Redis loss needs enforcing
    somewhere else.
    """

    scope = "public_api"

    def get_cache_key(self, request, view):
        key = getattr(request, "auth", None)
        if not isinstance(key, ApiKey):
            return None  # Not throttled here; HasApiKey rejects it anyway.
        return self.cache_format % {"scope": self.scope, "ident": str(key.id)}

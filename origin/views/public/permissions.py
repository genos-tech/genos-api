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
from origin.services.throttles import RecordedThrottleMixin
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


class PublicApiThrottle(RecordedThrottleMixin, throttling.SimpleRateThrottle):
    """Per-KEY rate limiting.

    DRF's `UserRateThrottle` would key on the user, so a person's five
    integrations would share one bucket and a runaway script would starve
    the others. The key is the unit an integrator actually controls, so
    it is the unit that gets the budget — and revoking a key frees it.

    ⚠️ **This fails OPEN.** A Redis outage means *unlimited*, not zero
    (`fail_closed` is off; since MIP-724 it at least logs each allow).
    That is the deliberate house policy for quota
    (`search_engine/quota.py` states it), and it is the right trade for
    the app — but it is worth knowing that the public API's rate limit is
    an availability measure, not a security control. Anything that must
    hold under Redis loss needs enforcing somewhere else.
    """

    scope = "public_api"

    def get_cache_key(self, request, view):
        credential = credential_of(request)
        if credential is None:
            return None  # Not throttled here; the permission rejects it anyway.
        # An API key's bucket is its id, as it always was; another kind brings its own,
        # so two kinds can never share one.
        return self.cache_format % {"scope": self.scope, "ident": credential.throttle_ident()}

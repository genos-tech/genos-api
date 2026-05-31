"""Runtime config endpoint.

Serves the per-user view of feature-flag rollout thresholds. The client
polls this on app boot and every 60s; the per-chat-type rollout flags
(`use_new_chat.dm/.gm/.mdm/.pm`), the global panic switch, and any
future kill switches all flow through here.

Contract:

    GET /api/v2/runtime-config

    Response (200):
        {
          "version": 1,
          "use_new_chat": {
            "dm":  <int 0-10000>,
            "gm":  <int 0-10000>,
            "mdm": <int 0-10000>,
            "pm":  <int 0-10000>
          },
          "panic_switch": <bool>
        }

Thresholds are basis-of-10000 (0 = off, 10000 = 100% of users). The
client computes its own bucket via `sha256(user_id + flag_name) % 10000`
and enables the flag when its bucket is strictly less than the threshold.
This keeps the bucketing decision client-side and stable per user — the
same user-id always hashes to the same bucket, no boundary flapping
mid-session even if the threshold changes between polls.

Values come from Django settings for now (env-var driven). A follow-up
will read them from Redis so they can be flipped from the admin console
without a deploy — but the wire contract this endpoint serves stays the
same; only the source-of-truth changes.

Auth: AuthenticatedAPIView. Signed-out callers get 401. The endpoint
does NOT echo back the user's bucketing decision — only the thresholds.
The decision is computed client-side so the rollout is fail-closed: a
network failure on this endpoint leaves the client on its previous
config (which initially is "all flags off").
"""

from django.conf import settings
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from rest_framework import status
from rest_framework.response import Response

# Defaults wire into the wire contract when settings.RUNTIME_CONFIG is
# absent or missing keys. Keep these conservative ("everything off") so
# a misconfigured deploy fails closed rather than open.
_DEFAULTS = {
    "version": 1,
    "use_new_chat": {
        "dm": 0,
        "gm": 0,
        "mdm": 0,
        "pm": 0,
    },
    "panic_switch": False,
}


def _read_config() -> dict:
    """Merge settings overrides into the conservative defaults.

    `settings.RUNTIME_CONFIG` is an optional dict; any key it sets
    wins, any key it omits falls back to `_DEFAULTS`. Nested dicts
    merge one level deep (sufficient for `use_new_chat`).
    """
    overrides = getattr(settings, "RUNTIME_CONFIG", None) or {}
    merged = dict(_DEFAULTS)
    for k, default_v in _DEFAULTS.items():
        if k in overrides:
            v = overrides[k]
            if isinstance(default_v, dict) and isinstance(v, dict):
                merged[k] = {**default_v, **v}
            else:
                merged[k] = v
    return merged


class RuntimeConfigView(AuthenticatedAPIView):
    """GET /api/v2/runtime-config — return the current rollout config.

    Idempotent, cheap, and authoritative. The client polls every 60s;
    a network failure leaves it on its previous cached config rather
    than reverting to defaults, so a misbehaving rollout endpoint
    doesn't drag users back through the upgrade transition.
    """

    def get(self, request):
        return Response(_read_config(), status=status.HTTP_200_OK)

"""Observability middleware for the (intended-to-be-retired) legacy chat
REST endpoints.

After the chat v3 migration completed on the FE, every chat surface
routes through the unified `/api/v3/channels/...` API. The legacy
`/api/v2/{dm,gm,pm,mdm}/...` and `/api/v2/chat/...` routes are still
mounted but no longer expected to receive traffic.

Before the cleanup tracks delete them outright, we need confidence that
nothing — no straggler client, no integration test, no admin script —
is still calling them. This middleware:

  1. Logs a `WARNING` line on every hit so the deletion gate can grep
     server logs for the trailing window.
  2. Increments in-memory counters (per path-prefix) so an admin endpoint
     can render a one-shot health check without trawling logs.

The counters are process-local and reset on every restart — that's a
feature, not a bug. They're sized for short verification windows
(hours to days), not for long-term metrics. For longer windows, rely on
the log stream.

Bypass cost: a single dict lookup + string prefix check per request.
Effectively free; safe to leave mounted permanently.
"""

import logging
import threading
from collections import defaultdict

logger = logging.getLogger(__name__)


# Prefixes that should have zero traffic post-v3. Order matters: longest
# match wins so a hit on `/api/v2/chat/activity/...` lights up
# `chat/activity/` (which IS still expected) rather than the broader
# `chat/` bucket. The active path here is intentional — activity is the
# only `/api/v2/chat/...` family that still has traffic.
LEGACY_CHAT_PREFIXES = (
    "/api/v2/dm/",
    "/api/v2/gm/",
    "/api/v2/pm/",
    "/api/v2/mdm/",
    "/api/v2/chat-master/",
    "/api/v2/chat-attachment/",
)

# These chat-related paths are STILL ACTIVE post-v3. They're listed
# explicitly so the middleware doesn't lump them in with the dead
# legacy buckets (otherwise the deletion gate would never go green).
LEGACY_CHAT_EXCLUDED = (
    "/api/v2/chat/activity/",  # activity feed is its own domain
    "/api/v2/chat/read/",  # read-cursor mutation (worker handler still active)
)


_lock = threading.Lock()
_counters = defaultdict(int)  # prefix -> hit count
_first_seen = {}  # prefix -> ISO timestamp of first hit since boot
_last_seen = {}  # prefix -> ISO timestamp of most recent hit


def _match_prefix(path):
    """Return the legacy prefix the path belongs to, or None.

    Excludes the still-active chat sub-paths so they don't get counted
    as dead traffic.
    """
    for excl in LEGACY_CHAT_EXCLUDED:
        if path.startswith(excl):
            return None
    for prefix in LEGACY_CHAT_PREFIXES:
        if path.startswith(prefix):
            return prefix
    return None


def get_counters_snapshot():
    """Read-only snapshot of the in-process counters. Used by the admin
    endpoint to render a one-shot status without exposing the mutable
    dicts directly."""
    with _lock:
        return {
            "counters": dict(_counters),
            "first_seen": dict(_first_seen),
            "last_seen": dict(_last_seen),
        }


def reset_counters():
    """Drop all accumulated state. Useful when you want to start a fresh
    observation window without restarting the server."""
    with _lock:
        _counters.clear()
        _first_seen.clear()
        _last_seen.clear()


class LegacyChatTrafficMiddleware:
    """WSGI middleware that flags hits on legacy chat REST endpoints.

    Mounted alongside the other Django middlewares — no preferred
    position because we only need the resolved request path.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        prefix = _match_prefix(request.path)
        if prefix is not None:
            self._record(request, prefix)
        return self.get_response(request)

    def _record(self, request, prefix):
        from django.utils.timezone import now

        ts = now().isoformat()
        with _lock:
            _counters[prefix] += 1
            _first_seen.setdefault(prefix, ts)
            _last_seen[prefix] = ts
        # User-visible signal in the log stream. WARN (not INFO) so the
        # deletion gate's log query can be specific: any hit at all is
        # an alert-worthy event.
        user = getattr(request, "user", None)
        user_id = getattr(user, "id", None) if user is not None else None
        logger.warning(
            "[legacy-chat-traffic] %s %s prefix=%s user=%s",
            request.method,
            request.path,
            prefix,
            user_id,
        )

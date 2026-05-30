"""Read-only admin endpoint for the legacy-chat-traffic counters.

Pairs with `origin.middleware.legacy_chat_traffic` — the middleware
counts hits in process memory, this view exposes them so the deletion
gate can render a one-shot status without trawling logs.

Contract:

    GET /api/admin/legacy-chat-traffic

    Response (200):
        {
          "counters": {
            "/api/v2/dm/": <int>,
            "/api/v2/gm/": <int>,
            ...
          },
          "first_seen": {"<prefix>": "<iso>"},
          "last_seen":  {"<prefix>": "<iso>"},
          "process_pid": <int>,
        }

Auth: same Bearer/JWT auth as the rest of `/api/...`. We deliberately
don't gate by Django staff/superuser because the response is read-only
and only meaningful to engineers — surfacing it via the standard auth
keeps the operational story simple for dev environments.

Counters are process-local, so a multi-worker deployment reports per
worker. The deletion gate aggregates across workers via log search
(the WARN line carries the same data) rather than relying on this
endpoint alone.
"""

import os

from rest_framework.response import Response

from origin.middleware.legacy_chat_traffic import (
    LEGACY_CHAT_EXCLUDED,
    LEGACY_CHAT_PREFIXES,
    get_counters_snapshot,
    reset_counters,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class LegacyChatTrafficView(AuthenticatedAPIView):
    def get(self, request):
        snap = get_counters_snapshot()
        return Response(
            {
                "process_pid": os.getpid(),
                "watched_prefixes": list(LEGACY_CHAT_PREFIXES),
                "excluded_prefixes": list(LEGACY_CHAT_EXCLUDED),
                "counters": snap["counters"],
                "first_seen": snap["first_seen"],
                "last_seen": snap["last_seen"],
            }
        )

    def delete(self, request):
        """Reset accumulated counters without restarting the server.

        Useful when starting a fresh observation window after a release
        or after fixing a bug that was generating false positives.
        """

        reset_counters()
        return Response({"reset": True})

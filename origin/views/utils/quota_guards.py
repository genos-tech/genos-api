"""Pre-flight tier-quota guards for REST create endpoints.

Mirrors the agent-ask gate in `agent_views.py`: check before the
write, return HTTP 429 with the standard limit payload when the
user's monthly cap is exhausted. The underlying
`check_remaining_monthly` fails OPEN (returns "allowed" on any infra
error), so these guards can never block a write because a counter
lookup hiccuped.

Usage (walrus pattern, same as the request validators):

    if res := check_monthly_creation_quota(request.user.id, TASK_CREATE_KEY, "task_create"):
        return res
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.quota import check_remaining_monthly

_CATEGORY_NOUNS = {
    "task_create": "tasks",
    "note_create": "notes",
}


def check_monthly_creation_quota(user_id, key: str, category: str, n: int = 1) -> Response | None:
    """Return a 429 Response when the monthly cap is exhausted, else None.

    `n` = units the caller is about to create (bulk creators pass the
    batch size). Response shape matches the agent-ask 429 so the
    frontend handles both identically:
    `{"error", "limit_reached": true, "used", "limit", "category"}`.
    """
    allowed, used, limit = check_remaining_monthly(str(user_id), key, n=n)
    if allowed:
        return None
    noun = _CATEGORY_NOUNS.get(category, "items")
    return Response(
        {
            "error": (
                f"You've created all {limit} {noun} for this month. "
                "Upgrade your plan to keep going."
            ),
            "limit_reached": True,
            "used": used,
            "limit": limit,
            "category": category,
        },
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )

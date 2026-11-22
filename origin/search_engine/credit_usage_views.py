"""GET /api/v2/agent/credits/usage/ — the caller's own AI credit usage (MIP-1971, MIP-2024)."""

from rest_framework import status
from rest_framework.response import Response

from origin.search_engine import credit_usage
from origin.search_engine.quota import resolve_effective_tier
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class CreditUsageView(AuthenticatedAPIView):
    """Balance, renewal, this month's usage by job and a page of recent charges.

        GET /api/v2/agent/credits/usage/[?limit=<n>]
            the whole payload; `recent` is the newest `limit` charges (5 by default, 25 at most)
        GET /api/v2/agent/credits/usage/?cursor=<id>[&limit=<n>]
            only `{recent, next_cursor}`: the charges after that position

    `next_cursor` is the `cursor` for the next page and null on the last one.

    CALLER ONLY: the user is `request.user`; the endpoint takes no user, team or id parameter (the
    cursor is a position inside the caller's own charges), so nobody, a team owner included, can
    read another member's usage through it.
    """

    def get(self, request):
        user = request.user
        if not getattr(user, "pk", None):
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)
        limit = credit_usage.clamp_limit(request.query_params.get("limit"))
        raw_cursor = request.query_params.get("cursor")
        if raw_cursor is not None:
            try:
                cursor = int(raw_cursor)
            except ValueError:
                cursor = 0
            if cursor <= 0:
                return Response(
                    {"error": "cursor must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows, next_cursor = credit_usage.recent_for(user, limit=limit, cursor=cursor)
            return Response({"recent": rows, "next_cursor": next_cursor})
        tier = resolve_effective_tier(str(user.pk))["tier"]
        return Response(credit_usage.usage_for(user, tier=tier, limit=limit))

"""GET /api/v2/agent/credits/usage/ — the caller's own AI credit usage (MIP-1971)."""

from rest_framework import status
from rest_framework.response import Response

from origin.search_engine import credit_usage
from origin.search_engine.quota import resolve_effective_tier
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class CreditUsageView(AuthenticatedAPIView):
    """Balance, renewal, this month's usage by job and the last 50 charges.

    CALLER ONLY: the user is `request.user`; the endpoint takes no user, team or id parameter, so
    nobody (a team owner included) can read another member's usage through it.
    """

    def get(self, request):
        user = request.user
        if not getattr(user, "pk", None):
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)
        tier = resolve_effective_tier(str(user.pk))["tier"]
        return Response(credit_usage.usage_for(user, tier=tier))

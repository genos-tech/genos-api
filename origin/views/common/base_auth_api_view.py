from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from origin.views.utils.foreign_team_scope import align_team_to_object


class AuthenticatedAPIView(APIView):
    """Base APIView that requires authentication for all requests."""

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        """Authenticate, then make `team_id` name the object's real owner.

        Cross-team sharing lets a caller work on another team's project,
        chat or note folder from inside their own workspace, so the
        `team_id` the client sends is their current team while the object
        belongs to the host. Every view here reads that parameter, and
        most of them filter on it. See `utils/foreign_team_scope` for why
        this is one substitution here rather than a fix at each site, and
        for why it cannot grant anything.

        After `super()`, so the caller is authenticated (and rate limits
        and permissions have run) before we look anything up on their
        behalf.
        """
        super().initial(request, *args, **kwargs)
        align_team_to_object(request)

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.views.common.object_body import ObjectBodyMixin
from origin.views.utils.foreign_team_scope import align_team_to_object
from origin.views.utils.malformed_lookup import MALFORMED_ID_ERROR, is_malformed_lookup_value

logger = logging.getLogger(__name__)


class AuthenticatedAPIView(ObjectBodyMixin, APIView):
    """Base APIView that requires authentication for all requests.

    A JSON body must be an object (`object_body.ObjectBodyMixin`): the
    handler never sees a list, a string or a number, and a malformed body
    is a 400 rather than an empty one.
    """

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
        behalf, and so the body guard has run: the alignment reads the
        body too, and swallows a parse error, so before the guard it read
        a malformed body as an empty one and the view did the same.
        """
        super().initial(request, *args, **kwargs)
        align_team_to_object(request)

    def handle_exception(self, exc):
        """A malformed id that reached a lookup is a 400, not a 500 (MIP-1060).

        A client-supplied `team_id`, `project_id` or `id` goes into an ORM filter in
        many views, and the field refuses what it cannot parse. This is the net under
        the views that parse first (and answer in their own words): it catches only
        Django's own two parse errors (`malformed_lookup`), logs each one so a route
        that needs a proper answer stays visible, and leaves every other exception to
        the normal 500.
        """
        if is_malformed_lookup_value(exc):
            request = getattr(self, "request", None)
            logger.warning(
                "malformed id answered 400: %s %s: %s",
                getattr(request, "method", "?"),
                getattr(request, "path", "?"),
                exc,
            )
            return Response({"error": MALFORMED_ID_ERROR}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)

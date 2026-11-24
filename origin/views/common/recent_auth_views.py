"""Check the signed-in member's password again (MIP-2240).

    POST /api/v2/user/confirm-password/   {"password": "..."}
        200 {"recent_auth_token": "...", "expires_in": 600}
        403 {"error": "Password is incorrect."}

The token is what `api-keys`, `webhooks` and the AI-app consent ask for (see
`origin.services.recent_auth`). JWT only, as key management is: an API key must not be able to
unlock the creation of further ones. This is a password oracle for whoever holds a session, so
it is throttled per member and fails closed.
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.services import recent_auth
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)


class ConfirmPasswordBurstThrottle(WindowRateThrottle):
    scope = "confirm_password_burst"
    rate = "5/min"
    fail_closed = True


class ConfirmPasswordSustainedThrottle(WindowRateThrottle):
    scope = "confirm_password_sustained"
    rate = "30/hour"
    fail_closed = True


class ConfirmPasswordView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [ConfirmPasswordBurstThrottle, ConfirmPasswordSustainedThrottle]

    def post(self, request):
        user = request.user
        if not recent_auth.required_for(user):
            # Nothing to type (Google or GitHub sign-in): the gate does not apply, so say so
            # and hand out no token.
            return Response({"required": False}, status=status.HTTP_200_OK)
        password = request.data.get("password")
        # A non-string would raise out of `check_password` (a 500 on request input).
        if not isinstance(password, str) or not password or not user.check_password(password):
            logger.warning("[recent-auth] password not confirmed user=%s", user.pk)
            return Response({"error": "Password is incorrect."}, status=status.HTTP_403_FORBIDDEN)
        return Response(
            {
                "required": True,
                "recent_auth_token": recent_auth.issue(user),
                "expires_in": recent_auth.WINDOW_SECONDS,
            },
            status=status.HTTP_200_OK,
        )

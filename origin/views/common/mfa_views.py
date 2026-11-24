"""Two-factor sign-in endpoints (MIP-2239). The rules and the reasons are in `services/mfa.py`.

Five endpoints for the signed-in person (Settings > Security) and one public one, the second
step of sign-in. Every answer that is a refusal carries a stable `code` the client maps to its
own words; none of them echoes a secret, a code or a hash.
"""

from __future__ import annotations

import logging

from django.contrib.auth.models import update_last_login
from django.http import JsonResponse
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.settings import api_settings as jwt_api_settings

from origin.models.common.security_event_models import SecurityAction
from origin.serializers.common.user_serializers import (
    CustomTokenObtainPairSerializer,
    UserSerializer,
)
from origin.services import mfa
from origin.services.security_events import record_security_event
from origin.services.throttles import AuthThrottle, WindowRateThrottle, with_defaults
from origin.views.common.object_body import ObjectBodyMixin
from origin.views.utils.request_validators import body_object

from .auth_views import _set_refresh_cookie, _signed_in_response
from .base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)


class MfaSignInBurstThrottle(AuthThrottle):
    """The second step of sign-in, per address. The per-account lock is the real limit
    (a guesser rotates addresses); this keeps volume off the database."""

    rate = "20/min"
    scope = "mfa_signin_burst"


class MfaSignInSustainedThrottle(AuthThrottle):
    rate = "200/hour"
    scope = "mfa_signin_sustained"


class MfaManageThrottle(WindowRateThrottle):
    """Setup, confirm, disable and new recovery codes, per person. Three of the four take
    the password, so each is a place to guess it from a hijacked session: refuse with the
    cache down rather than allow."""

    rate = "30/hour"
    scope = "mfa_manage"
    fail_closed = True


def _refusal(code: str, message: str, http_status: int, **extra) -> Response:
    return Response({"code": code, "error": message, **extra}, status=http_status)


def _locked(exc: mfa.Locked) -> Response:
    response = _refusal(
        "mfa_locked",
        "Too many wrong codes. Try again later.",
        status.HTTP_429_TOO_MANY_REQUESTS,
        retry_after=exc.retry_after_seconds,
    )
    response["Retry-After"] = str(exc.retry_after_seconds)
    return response


def _password_ok(request) -> bool:
    password = body_object(request).get("password")
    return isinstance(password, str) and request.user.check_password(password)


def _bad_password() -> Response:
    return _refusal("bad_password", "Password is incorrect.", status.HTTP_403_FORBIDDEN)


class MfaStatusView(AuthenticatedAPIView):
    """Whether two-factor is on for the caller, and how many recovery codes are left."""

    def get(self, request):
        s = mfa.status_for(request.user)
        return Response(
            {
                "enabled": s.enabled,
                "eligible": s.eligible,
                "enabled_at": s.enabled_at.isoformat() if s.enabled_at else None,
                "recovery_codes_remaining": s.recovery_codes_remaining,
            }
        )


class MfaSetupView(AuthenticatedAPIView):
    """Start setup: needs the password, returns the secret and the QR link once."""

    throttle_classes = with_defaults(MfaManageThrottle)

    def post(self, request):
        if not mfa.is_eligible(request.user):
            return _refusal(
                "not_eligible",
                "This account signs in with a provider, which has its own second factor.",
                status.HTTP_400_BAD_REQUEST,
            )
        if not _password_ok(request):
            return _bad_password()
        try:
            secret, uri = mfa.begin_setup(request.user)
        except mfa.AlreadyEnabled:
            return _refusal(
                "already_enabled", "Two-factor sign-in is already on.", status.HTTP_409_CONFLICT
            )
        response = Response({"secret": secret, "otpauth_uri": uri})
        response["Cache-Control"] = "no-store"
        return response


class MfaEnableView(AuthenticatedAPIView):
    """Finish setup with the first code from the app. Returns the recovery codes, once."""

    throttle_classes = with_defaults(MfaManageThrottle)

    def post(self, request):
        code = body_object(request).get("code")
        try:
            codes = mfa.confirm_setup(
                request.user, code if isinstance(code, str) else "", request=request
            )
        except mfa.NoSetupInProgress:
            return _refusal("no_setup", "Start setup first.", status.HTTP_400_BAD_REQUEST)
        except mfa.AlreadyEnabled:
            return _refusal(
                "already_enabled", "Two-factor sign-in is already on.", status.HTTP_409_CONFLICT
            )
        except mfa.Locked as exc:
            return _locked(exc)
        except mfa.InvalidCode:
            return _refusal("invalid_code", "That code is not right.", status.HTTP_400_BAD_REQUEST)
        response = Response({"enabled": True, "recovery_codes": codes})
        response["Cache-Control"] = "no-store"
        return response


class MfaDisableView(AuthenticatedAPIView):
    """Turn it off. Needs the password, so a hijacked session alone cannot."""

    throttle_classes = with_defaults(MfaManageThrottle)

    def post(self, request):
        if not _password_ok(request):
            return _bad_password()
        try:
            mfa.disable(request.user, request=request)
        except mfa.NotEnabled:
            return _refusal(
                "not_enabled", "Two-factor sign-in is not on.", status.HTTP_409_CONFLICT
            )
        return Response({"enabled": False})


class MfaRecoveryCodesView(AuthenticatedAPIView):
    """Replace the recovery codes with a new set (the old ones stop working). Needs the password."""

    throttle_classes = with_defaults(MfaManageThrottle)

    def post(self, request):
        if not _password_ok(request):
            return _bad_password()
        try:
            codes = mfa.regenerate_recovery_codes(request.user, request=request)
        except mfa.NotEnabled:
            return _refusal(
                "not_enabled", "Two-factor sign-in is not on.", status.HTTP_409_CONFLICT
            )
        response = Response({"recovery_codes": codes})
        response["Cache-Control"] = "no-store"
        return response


class SignInMfaView(ObjectBodyMixin, APIView):
    """The second step of sign-in: the pending token from `POST /signin/` plus a code.

    Only this mints the session. The pending token is the sole credential here, and it proves
    the password step passed for exactly one account within the last five minutes.
    """

    permission_classes = [permissions.AllowAny]
    # Neither a cookie nor a bearer header means anything here.
    authentication_classes: list = []
    throttle_classes = [MfaSignInBurstThrottle, MfaSignInSustainedThrottle]

    def post(self, request):
        body = body_object(request)
        user = mfa.read_pending_token(body.get("mfa_token"))
        if user is None:
            return _refusal(
                "mfa_token_invalid",
                "That sign-in expired. Enter your password again.",
                status.HTTP_401_UNAUTHORIZED,
            )
        code = body.get("code")
        try:
            method = mfa.verify_second_factor(
                user, code if isinstance(code, str) else "", request=request
            )
        except mfa.Locked as exc:
            return _locked(exc)
        except mfa.InvalidCode:
            return _refusal("invalid_code", "That code is not right.", status.HTTP_401_UNAUTHORIZED)
        except mfa.NotEnabled:
            return _refusal(
                "mfa_token_invalid",
                "That sign-in expired. Enter your password again.",
                status.HTTP_401_UNAUTHORIZED,
            )

        refresh = CustomTokenObtainPairSerializer.get_token(user)
        if jwt_api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, user)
        record_security_event(
            SecurityAction.SIGNIN_SUCCEEDED,
            request=request,
            actor=user,
            metadata={"method": "password", "second_factor": method},
        )
        response = JsonResponse(
            _signed_in_response(UserSerializer(user).data, str(refresh.access_token), str(refresh))
        )
        _set_refresh_cookie(response, str(refresh))
        return response

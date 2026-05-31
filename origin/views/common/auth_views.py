import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from origin.services.demo_seeder import (
    create_demo_environment,
    delete_demo_environment,
    kick_off_demo_reindex,
)
from rest_framework import permissions, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)


def _set_refresh_cookie(response, refresh_value: str) -> None:
    """Centralised helper so every place that issues a refresh cookie
    uses the exact same SameSite/Secure/HttpOnly attributes. Different
    attributes between set/delete cause the browser to keep stale
    cookies in production."""
    response.set_cookie(
        key="refresh",
        value=refresh_value,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


from origin.serializers.common.user_serializers import (
    CustomTokenObtainPairSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ResendVerificationSerializer,
    UserCreateSerializer,
    UserSerializer,
)
from origin.services.email import send_templated_email

User = get_user_model()


class UserViewSet(viewsets.ModelViewSet):
    """User ViewSet"""

    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]  # Allow anyone to register

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        """Register a user. For email/password signups, send a verification
        link instead of issuing JWTs — the user must click the link before
        they can sign in. Non-email paths (none currently use this endpoint
        but kept defensively) still receive immediate tokens."""
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"message": serializer.errors.items()},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = serializer.save()

        # System users (created internally for project automations) are
        # not real humans with a real inbox — they're pre-verified and
        # still get a JWT so the caller can drive the rest of the flow.
        if user.primary_auth_provider == "email" and not user.is_system_user:
            raw = _issue_verification_token(user)
            try:
                _send_verification_email(user, raw)
            except Exception as exc:
                # Don't fail the signup; user can request a resend.
                logger.exception("Verification email send failed: %s", exc)
            return Response(
                {"message": "verification_email_sent", "email": user.email},
                status=status.HTTP_201_CREATED,
            )

        # System users + (defensively) non-email providers — return JWT
        # for immediate use. Mark verified so the sign-in gate doesn't
        # block their next login.
        if user.is_system_user and not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {"username": user.username, "email": user.email, "id": user.id},
                "message": "User creation completed",
            },
            status=status.HTTP_201_CREATED,
        )


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer  # Use custom serializer

    def post(self, request, *args, **kwargs):
        # Email-verification gate. We check before delegating to
        # `super().post()` so we never mint a token for an unverified
        # account. The `check_password` guard ensures `email_not_verified`
        # is only revealed once credentials are otherwise valid — wrong
        # passwords still flow through to the normal 401 below, so this
        # doesn't leak account existence.
        email = (request.data or {}).get("email")
        password = (request.data or {}).get("password")
        if email and password:
            unverified = User.objects.filter(email__iexact=email).first()
            if (
                unverified is not None
                and unverified.primary_auth_provider == "email"
                and not unverified.is_email_verified
                and unverified.check_password(password)
            ):
                return JsonResponse(
                    {
                        "detail": "email_not_verified",
                        "email": unverified.email,
                    },
                    status=403,
                )

        response = super().post(request, *args, **kwargs)
        data = response.data

        # Create JSON response (access token sent in response, refresh token in cookie)
        response = JsonResponse(
            {
                "access": data["access"],
                "refresh": data["refresh"],
                "username": data["user"]["username"],
                "user_id": data["user"]["id"],
                "email": data["user"]["email"],
                "profile_image_file_name": data["user"]["profile_image_file_name"],
                "ts_joined_at": data["user"]["ts_created_at"],
                "is_offline_forced": data["user"]["is_offline_forced"],
                "role": data["user"]["role"],
                "base_country": data["user"]["base_country"],
                "custom_status": data["user"]["custom_status"],
            }
        )

        # Set the refresh token in a httpOnly cookie. SameSite/Secure
        # come from settings so the cookie is `SameSite=None; Secure`
        # in production (where the frontend host differs from the API
        # host) and `SameSite=Lax; Secure=False` in local dev (where
        # plain-HTTP localhost can't send Secure cookies).
        _set_refresh_cookie(response, data["refresh"])

        return response


class DemoSignInThrottle(AnonRateThrottle):
    """Per-IP throttle for the unauthenticated demo signin endpoint.

    Each call seeds ~150 rows, so without a cap a botnet could exhaust
    the DB. 10/hour per IP is generous for legitimate evaluation while
    making volumetric abuse impractical.
    """

    rate = "10/hour"
    scope = "demo_signin"


class DemoSignInView(APIView):
    """Provision a one-click demo user, team, and seeded sample data.

    Mirrors the response shape of `CustomTokenObtainPairView` so the
    frontend can re-use its existing post-signin handler, plus adds
    `team_id` / `team_name` / `is_demo` so the client can skip the
    team-picker (`/jointeam`) and land directly in `/workspace`.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [DemoSignInThrottle]

    def post(self, request: Request):
        short = uuid.uuid4().hex[:8]
        email = f"demo+{short}@genos.app"
        username = f"Demo User {short}"

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    username=username,
                    password=secrets.token_urlsafe(32),
                    is_demo=True,
                    is_email_verified=True,
                )
                team_info = create_demo_environment(user)
        except Exception as exc:
            logger.exception("Demo signin failed: %s", exc)
            return Response(
                {"error": "Failed to create demo environment. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # The seeded chats/tasks/notes need to land in OpenSearch before
        # the demo user can use Spotlight (Cmd-K AI). The scheduled cron
        # only runs every 10 min — too slow for a first-impression demo.
        # Fire an incremental reindex on a background thread so the
        # signin response stays fast; failures are logged but non-fatal.
        kick_off_demo_reindex()

        refresh = RefreshToken.for_user(user)
        response_data = {
            "access": str(refresh.access_token),
            "username": user.username,
            "user_id": str(user.id),
            "email": user.email,
            "profile_image_file_name": user.profile_image_file_name or "",
            "ts_joined_at": user.ts_created_at.isoformat() if user.ts_created_at else "",
            "is_offline_forced": user.is_offline_forced,
            "role": user.role or "",
            "base_country": user.base_country or "",
            "custom_status": user.custom_status or "",
            "team_id": team_info["team_id"],
            "team_name": team_info["team_name"],
            "is_demo": True,
        }
        response = JsonResponse(response_data, status=201)
        _set_refresh_cookie(response, str(refresh))
        return response


class CookieTokenRefreshView(TokenRefreshView):
    def get(self, request: Request, *args, **kwargs):
        refresh = request.COOKIES.get("refresh")  # Get refresh token from cookies

        if not refresh or refresh == "":
            return Response({"error": "No refresh token provided"}, status=403)

        # Manually create the serializer with the refresh token
        serializer = TokenRefreshSerializer(data={"refresh": refresh})

        try:
            serializer.is_valid(raise_exception=True)
            validated = serializer.validated_data

            # Drop the (now-rotated, soon-to-be-blacklisted) refresh
            # value out of the JSON body — the client should rely on
            # the cookie for refresh, not see the value in JS land.
            new_refresh = validated.pop("refresh", None)
            response = Response(validated)

            # `ROTATE_REFRESH_TOKENS=True` means simplejwt issues a
            # fresh refresh token on every successful refresh. Persist
            # it back into the httpOnly cookie so subsequent refreshes
            # use the new (non-blacklisted) token. Without this the
            # second refresh after a rotation would 403 once
            # `BLACKLIST_AFTER_ROTATION` actually starts blacklisting
            # the old one.
            if new_refresh:
                _set_refresh_cookie(response, new_refresh)
            return response
        except (InvalidToken, TokenError):
            return Response({"error": "Invalid or expired refresh token"}, status=403)


class LogoutView(APIView):
    """Sign-out endpoint.

    `permission_classes = [AllowAny]` because the client may be
    calling this with an already-expired access token (or no Bearer
    header at all in the cross-tab case); the cookie is what
    identifies the session being ended.

    On success we both blacklist the refresh token server-side (so it
    cannot be replayed even if it leaks) AND clear the cookie on the
    browser using the same SameSite/Secure attrs it was set with —
    otherwise some browsers won't actually evict it.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request: Request):
        refresh = request.COOKIES.get("refresh")

        # Identify the user *before* blacklisting so we can clean up
        # demo accounts. LogoutView is AllowAny and the frontend
        # logout fetch does not currently attach a Bearer header, so
        # we look at the JWT first (in case it's there) and fall back
        # to decoding the refresh cookie's claims.
        user = None
        try:
            auth_result = JWTAuthentication().authenticate(request)
            if auth_result:
                user = auth_result[0]
        except Exception:
            pass
        if user is None and refresh:
            try:
                user_id = RefreshToken(refresh).get("user_id")
                user = User.objects.filter(id=user_id).first()
            except (InvalidToken, TokenError, KeyError, AttributeError):
                pass

        if refresh:
            try:
                # `.blacklist()` requires the `token_blacklist` app
                # to be in INSTALLED_APPS and its migrations to have
                # been run. Wrap in try/except so a malformed /
                # already-expired token doesn't 500 the logout.
                RefreshToken(refresh).blacklist()
            except (InvalidToken, TokenError, AttributeError):
                # Fall through: even if we can't blacklist, we still
                # want to clear the cookie below so the browser stops
                # sending it.
                pass

        # Demo users are deleted on signout so each demo session is
        # isolated and the DB doesn't accumulate abandoned demos. The
        # daily cron sweeps anything that slipped through. Cleanup is
        # best-effort — never 500 the logout if it fails.
        if user is not None and getattr(user, "is_demo", False):
            try:
                delete_demo_environment(user)
            except Exception as exc:
                logger.exception("Demo cleanup on signout failed: %s", exc)

        response = JsonResponse({"message": "Signed out"})
        response.delete_cookie(
            "refresh",
            samesite=settings.AUTH_COOKIE_SAMESITE,
        )
        return response


class UserInfoView(AuthenticatedAPIView):
    def get(self, request):
        user = request.user  # Get the currently authenticated user
        serializer = UserSerializer(user)  # Serialize user data
        return Response(serializer.data)


class PasswordResetRequestView(APIView):
    """Accept an email, mint a one-time reset token, and email a link.

    Always returns 200 — even if the email isn't registered — to
    prevent user-enumeration via response or timing differences. The
    token is stored as a SHA-256 hash so a DB read can't recover the
    live URL.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request: Request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = User.objects.filter(email__iexact=email, is_deleted=False).first()
        if user is not None:
            token = secrets.token_urlsafe(32)
            user.password_reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
            user.password_reset_token_expires_at = timezone.now() + timedelta(
                minutes=settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES
            )
            user.save(
                update_fields=[
                    "password_reset_token_hash",
                    "password_reset_token_expires_at",
                ]
            )
            reset_url = f"{settings.FRONTEND_BASE_URL}/reset-password?token={token}"
            try:
                send_templated_email(
                    to=user.email,
                    subject="Reset your Genos password",
                    template_base="password_reset",
                    context={
                        "reset_url": reset_url,
                        "expiry_minutes": settings.PASSWORD_RESET_TOKEN_EXPIRY_MINUTES,
                        "user_name": user.username,
                    },
                )
            except Exception as exc:
                # Don't leak SMTP failures to the client (would also
                # leak that the email exists). Log for ops to chase.
                logger.exception("Password reset email send failed: %s", exc)

        return Response(status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """Consume a reset token + set the new password.

    Looks the token up by its SHA-256 hash (so DB reads can't recover
    live tokens), checks expiry, runs Django's full password validator
    chain, then clears the token so it can't be reused.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request: Request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        new_password = serializer.validated_data["new_password"]

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = User.objects.filter(
            password_reset_token_hash=token_hash,
            password_reset_token_expires_at__gt=timezone.now(),
            is_deleted=False,
        ).first()
        if user is None:
            return Response(
                {"detail": "Invalid or expired token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            return Response(
                {"detail": " ".join(exc.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.password_reset_token_hash = None
        user.password_reset_token_expires_at = None
        user.save(
            update_fields=[
                "password",
                "password_reset_token_hash",
                "password_reset_token_expires_at",
            ]
        )
        return Response(status=status.HTTP_200_OK)


def _issue_verification_token(user) -> str:
    """Mint a fresh verification token, hash it onto the user row, and
    return the raw token for the URL. Overwrites any existing token so
    the most recent link in the user's inbox is the only one that works.
    """
    raw = secrets.token_urlsafe(32)
    user.email_verification_token_hash = hashlib.sha256(raw.encode()).hexdigest()
    user.email_verification_token_expires_at = timezone.now() + timedelta(
        minutes=settings.EMAIL_VERIFICATION_TOKEN_EXPIRY_MINUTES
    )
    user.save(
        update_fields=[
            "email_verification_token_hash",
            "email_verification_token_expires_at",
        ]
    )
    return raw


def _send_verification_email(user, raw_token: str) -> None:
    verify_url = f"{settings.FRONTEND_BASE_URL}/verify-email?token={raw_token}"
    send_templated_email(
        to=user.email,
        subject="Verify your Genos email",
        template_base="email_verification",
        context={
            "verify_url": verify_url,
            "expiry_hours": settings.EMAIL_VERIFICATION_TOKEN_EXPIRY_MINUTES // 60,
            "user_name": user.username,
        },
    )


class VerifyEmailView(APIView):
    """Consume a one-time verification token from the email link.

    GET /api/v2/user/verify-email/?token=<token>

    The token is looked up by its SHA-256 hash and checked against the
    expiry timestamp. On success we flip `is_email_verified=True` and
    clear the token columns so the link can't be reused. The user is
    NOT signed in by this endpoint — they still need to authenticate
    via `/signin/` afterwards.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request: Request):
        token = request.query_params.get("token", "")
        if not token:
            return Response(
                {"detail": "invalid_or_expired"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = User.objects.filter(
            email_verification_token_hash=token_hash,
            email_verification_token_expires_at__gt=timezone.now(),
            is_email_verified=False,
            is_deleted=False,
        ).first()
        if user is None:
            return Response(
                {"detail": "invalid_or_expired"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.is_email_verified = True
        user.email_verification_token_hash = None
        user.email_verification_token_expires_at = None
        user.save(
            update_fields=[
                "is_email_verified",
                "email_verification_token_hash",
                "email_verification_token_expires_at",
            ]
        )
        return Response({"message": "verified"}, status=status.HTTP_200_OK)


class ResendVerificationView(APIView):
    """Re-send the verification email for an unverified email-password user.

    POST /api/v2/user/verify-email/resend/  body: {"email": "..."}

    Always returns 200 — mirrors PasswordResetRequestView to avoid
    leaking which addresses are registered or already verified. A fresh
    token is minted (invalidating any prior link), so the most recent
    email in the user's inbox is always the working one.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request: Request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = User.objects.filter(email__iexact=email, is_deleted=False).first()
        if (
            user is not None
            and user.primary_auth_provider == "email"
            and not user.is_email_verified
        ):
            raw = _issue_verification_token(user)
            try:
                _send_verification_email(user, raw)
            except Exception as exc:
                logger.exception("Resend verification email failed: %s", exc)
        return Response(status=status.HTTP_200_OK)

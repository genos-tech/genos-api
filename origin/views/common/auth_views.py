from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from rest_framework import viewsets, permissions
from rest_framework.views import APIView
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from rest_framework.request import Request
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from .base_auth_api_view import AuthenticatedAPIView


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
    UserSerializer,
    UserCreateSerializer,
    CustomTokenObtainPairSerializer,
)

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
        """Override user registration to return JWT token"""
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()

            # Generate JWT token
            refresh = RefreshToken.for_user(user)
            access_token = str(refresh.access_token)

            return Response(
                {
                    "access": access_token,
                    "refresh": str(refresh),
                    "user": {"username": user.username, "email": user.email, "id": user.id},
                    "message": "User creation completed",
                },
                status=status.HTTP_201_CREATED,
            )

        return Response({"message": serializer.errors.items()}, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer  # Use custom serializer

    def post(self, request, *args, **kwargs):
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

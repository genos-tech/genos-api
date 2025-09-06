from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken

from rest_framework import viewsets, permissions
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from rest_framework.request import Request
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from .base_auth_api_view import AuthenticatedAPIView
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
                "profile_image_url": data["user"]["profile_image_url"],
                "ts_joined_at": data["user"]["ts_created_at"],
                "custom_status": data["user"]["custom_status"],
                "status": data["user"]["status"],
            }
        )

        # Set the refresh token in a httpOnly cookie
        response.set_cookie(
            key="refresh",
            value=data["refresh"],
            httponly=True,
            secure=False,  # Change to True in production (HTTPS)
            samesite="Strict",
        )

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
            return Response(serializer.validated_data)  # Return new access token
        except (InvalidToken, TokenError):
            return Response({"error": "Invalid or expired refresh token"}, status=403)


class LogoutView(TokenRefreshView):
    def post(self, request):
        response = JsonResponse({"message": "Signed out"})
        response.delete_cookie("refresh")
        return response


class UserInfoView(AuthenticatedAPIView):
    def get(self, request):
        user = request.user  # Get the currently authenticated user
        serializer = UserSerializer(user)  # Serialize user data
        return Response(serializer.data)

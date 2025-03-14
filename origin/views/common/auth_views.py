from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from rest_framework import viewsets, permissions
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from origin.serializers.common.user_serializers import UserSerializer, UserCreateSerializer
from rest_framework.views import APIView

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
                    "user": UserSerializer(user).data,
                    "access_token": access_token,
                    "refresh_token": str(refresh),
                    "user": {"username": user.username, "email": user.email},
                },
                status=status.HTTP_201_CREATED,
            )

        return Response({"message": serializer.errors.items()}, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom JWT Login Serializer to include user data"""

    def validate(self, attrs):
        data = super().validate(attrs)

        user = self.user
        user_data = UserSerializer(user).data

        data.update(
            {
                "user": user_data,
                "access_token": data["access"],
                "refresh_token": data["refresh"],
                "user": user,
            }
        )

        return data


class CustomTokenObtainPairView(TokenObtainPairView):
    """Custom JWT Login View"""

    serializer_class = CustomTokenObtainPairSerializer


class UserInfoView(APIView):
    permission_classes = [IsAuthenticated]  # Requires authentication

    def get(self, request):
        user = request.user  # Get the currently authenticated user
        serializer = UserSerializer(user)  # Serialize user data
        return Response(serializer.data)

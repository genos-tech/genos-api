from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from origin.models.common import user_models


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = user_models.CustomUser
        fields = [
            "id",
            "username",
            "email",
            "phone_number",
            "profile_image_url",
            "profile_image_file_name",
            "is_offline_forced",
            "custom_status",
            "role",
            "base_country",
            "last_seen",
            "ts_created_at",
            "ts_updated_at",
            "token",
            "token_expiration",
            "ts_last_login_at",
            "groups",
            "user_permissions",
            "is_active",
            "is_staff",
        ]

        read_only_fields = [
            "id",
            "last_seen",
            "ts_created_at",
            "ts_updated_at",
            "token",
            "token_expiration",
            "ts_last_login_at",
        ]


class UserCreateSerializer(serializers.ModelSerializer):
    """Serializer for user registration"""

    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = user_models.CustomUser
        fields = ["id", "username", "email", "password", "is_system_user"]

    def create(self, validated_data):
        """Override create method to hash password"""
        user = user_models.CustomUser.objects.create_user(
            email=validated_data["email"],
            username=validated_data["username"],
            password=validated_data["password"],
            is_system_user=validated_data.get("is_system_user", False),
        )
        return user


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom JWT Login Serializer to include user data"""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username  # Add username to JWT payload
        token["email"] = user.email  # Add email to JWT payload
        token["profile_image_file_name"] = (
            user.profile_image_file_name
        )  # Add profile image file name
        return token

    def validate(self, attrs):
        data = super().validate(attrs)

        user = self.user
        user_data = UserSerializer(user).data

        data.update(
            {
                "user": user_data,
                "access": data["access"],
                "refresh": data["refresh"],
            }
        )

        return data

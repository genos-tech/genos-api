import hashlib

from django.utils import timezone
from origin.models.common import user_models
from origin.models.common.invite_models import TeamInvite
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


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
    # Optional team-invite token. When present it must be a live invite
    # whose invited_email matches this signup's email — validated up front
    # (in validate) so a bad/mismatched token rejects the request before
    # any user row is created, avoiding orphan accounts. The view consumes
    # the invite after save.
    invite_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = user_models.CustomUser
        fields = ["id", "username", "email", "password", "is_system_user", "invite_token"]

    def validate(self, attrs):
        token = (attrs.get("invite_token") or "").strip()
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            invite = TeamInvite.objects.filter(token_hash=token_hash).first()
            if invite is None or invite.status != "pending" or invite.expires_at <= timezone.now():
                raise serializers.ValidationError(
                    {"invite_token": "This invitation is invalid or has expired."}
                )
            if invite.invited_email.lower() != (attrs.get("email") or "").lower():
                raise serializers.ValidationError(
                    {"invite_token": "This invitation is for a different email address."}
                )
        return attrs

    def create(self, validated_data):
        """Override create method to hash password"""
        validated_data.pop("invite_token", None)
        user = user_models.CustomUser.objects.create_user(
            email=validated_data["email"],
            username=validated_data["username"],
            password=validated_data["password"],
            is_system_user=validated_data.get("is_system_user", False),
        )
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    """Request body for POST /password-reset/request/."""

    email = serializers.EmailField()


class ResendVerificationSerializer(serializers.Serializer):
    """Request body for POST /verify-email/resend/."""

    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Request body for POST /password-reset/confirm/.

    `min_length=8` mirrors UserCreateSerializer above; the view also runs
    Django's full AUTH_PASSWORD_VALIDATORS chain before saving.
    """

    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, min_length=8)


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

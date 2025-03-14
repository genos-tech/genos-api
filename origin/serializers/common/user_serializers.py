from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password

from origin.models.common import user_models


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = user_models.CustomUser
        fields = [
            "id",
            "username",
            "email",
            "profile_image_url",
            "status",
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
        fields = ["id", "username", "email", "password", "profile_image_url"]

    def create(self, validated_data):
        """Override create method to hash password"""
        user = user_models.CustomUser.objects.create_user(
            email=validated_data["email"],
            username=validated_data["username"],
            password=validated_data["password"],
            profile_image_url=validated_data.get("profile_image_url", ""),
        )
        return user


class ChatGroupSerializer(serializers.ModelSerializer):
    st_chat_group_name = serializers.CharField()
    bl_personal = serializers.BooleanField()
    id_owner = serializers.IntegerField()

    class Meta:
        model = user_models.ChatGroup
        fields = "__all__"

    def create(self, validated_data):
        chat_group = user_models.ChatGroup(
            st_chat_group_name=validated_data["st_chat_group_name"],
            bl_personal=validated_data["bl_personal"],
            id_owner=validated_data["id_owner"],
        )
        chat_group.save()
        return chat_group


class ChatGroupMemberSerializer(serializers.ModelSerializer):
    id_chat_group = serializers.IntegerField()
    id_user = serializers.IntegerField()
    dt_last_read = serializers.DateTimeField()

    class Meta:
        model = user_models.ChatGroupMember
        fields = "__all__"

    def create(self, validated_data):
        chat_group_member = user_models.ChatGroupMember(
            id_chat_group=validated_data["id_chat_group"],
            id_user=validated_data["id_user"],
            dt_last_read=validated_data["dt_last_read"],
        )
        chat_group_member.save()
        return chat_group_member

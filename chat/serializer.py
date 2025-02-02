from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password

from . import models


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = models.CustomUser
        fields = "__all__"

    def create(self, validated_data):
        user = models.CustomUser(
            username=validated_data["username"],
            email=validated_data["email"],
        )
        user.set_password(validated_data["password"])
        user.save()
        return user


class ChatGroupSerializer(serializers.ModelSerializer):
    st_chat_group_name = serializers.CharField()
    bl_personal = serializers.BooleanField()
    id_owner = serializers.IntegerField()

    class Meta:
        model = models.ChatGroup
        fields = "__all__"

    def create(self, validated_data):
        chat_group = models.ChatGroup(
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
        model = models.ChatGroupMember
        fields = "__all__"

    def create(self, validated_data):
        chat_group_member = models.ChatGroupMember(
            id_chat_group=validated_data["id_chat_group"],
            id_user=validated_data["id_user"],
            dt_last_read=validated_data["dt_last_read"],
        )
        chat_group_member.save()
        return chat_group_member

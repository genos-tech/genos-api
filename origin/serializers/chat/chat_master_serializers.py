from rest_framework import serializers
from origin.models.chat.chat_master_models import UserChatMaster


class UserChatMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserChatMaster
        fields = "__all__"

from rest_framework import serializers
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages


class GMMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = GMMaster
        fields = "__all__"


class GMMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = GMMembers
        fields = "__all__"


class GMMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = GMMessages
        fields = "__all__"


class GMThreadMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = GMThreadMessages
        fields = "__all__"

from rest_framework import serializers
from origin.models.chat.dm_models import DMMaster, DMMessages, DMThreadMessages


class DMMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = DMMaster
        fields = "__all__"


class DMMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = DMMessages
        fields = "__all__"


class DMThreadMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = DMThreadMessages
        fields = "__all__"

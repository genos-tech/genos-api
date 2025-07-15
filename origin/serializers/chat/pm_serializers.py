from rest_framework import serializers
from origin.models.chat.pm_models import PMMessages, PMThreadMessages


class PMMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = PMMessages
        fields = "__all__"


class PMThreadMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = PMThreadMessages
        fields = "__all__"

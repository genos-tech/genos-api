from rest_framework import serializers
from origin.models.chat.read_status_models import ReadStatus


class ReadStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReadStatus
        fields = "__all__"

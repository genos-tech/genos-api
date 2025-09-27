from argparse import Action
from rest_framework import serializers
from origin.models.chat.read_status_models import ReadStatus, ActivityReadStatus


class ReadStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReadStatus
        fields = "__all__"


class ActivityReadStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityReadStatus
        fields = "__all__"

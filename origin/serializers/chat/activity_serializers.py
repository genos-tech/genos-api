from rest_framework import serializers
from origin.models.chat.activity_models import ActivityFact


class ActivityFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityFact
        fields = "__all__"

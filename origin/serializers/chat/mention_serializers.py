from rest_framework import serializers
from origin.models.chat.mention_models import *


class MentionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentionFact
        fields = "__all__"

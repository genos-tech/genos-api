from rest_framework import serializers
from origin.models.chat.reaction_models import *


class ReactionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReactionFact
        fields = "__all__"

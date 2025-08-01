from rest_framework import serializers
from origin.models.common.reaction_models import *


class ReactionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReactionFact
        fields = "__all__"

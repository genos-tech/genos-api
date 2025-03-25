from rest_framework import serializers
from origin.models.common.team_models import TeamMaster, TeamMembers


class TeamMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamMaster
        fields = "__all__"


class TeamMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamMembers
        fields = "__all__"

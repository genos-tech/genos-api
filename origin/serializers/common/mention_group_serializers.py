from rest_framework import serializers

from origin.models.common.mention_group_models import (
    MentionGroupMaster,
    MentionGroupMembers,
)


class MentionGroupMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentionGroupMaster
        fields = "__all__"


class MentionGroupMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = MentionGroupMembers
        fields = "__all__"

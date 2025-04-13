from rest_framework import serializers
from origin.models.project.prj_models import *


class ProjectMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMaster
        fields = "__all__"


class ProjectMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMembers
        fields = "__all__"


class ProjectTagsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTags
        fields = "__all__"

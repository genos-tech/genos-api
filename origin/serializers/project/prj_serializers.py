from rest_framework import serializers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers


class ProjectMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMaster
        fields = "__all__"


class ProjectMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMembers
        fields = "__all__"

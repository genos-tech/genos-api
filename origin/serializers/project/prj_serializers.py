from rest_framework import serializers

from origin.models.project.prj_models import *


class ProjectMasterSerializer(serializers.ModelSerializer):
    # Whether the owner has the project archived (MIP-1693, MIP-1853). `archived_at` (the model
    # field, a model column) is the time of that archive, null when the project is live.
    archived = serializers.SerializerMethodField()

    class Meta:
        model = ProjectMaster
        fields = "__all__"

    def get_archived(self, obj) -> bool:
        return obj.archived_at is not None


class ProjectMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMembers
        fields = "__all__"


class ProjectTagsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTags
        fields = "__all__"


class ProjectTaskTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTaskTemplate
        fields = "__all__"


class ProjectSavedTaskFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectSavedTaskFilter
        fields = "__all__"

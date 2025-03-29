from rest_framework import serializers
from origin.models.task.task_models import *


class TaskMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskMaster
        fields = "__all__"


class TaskAttachmentsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskAttachments
        fields = "__all__"


class TaskTagsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskTags
        fields = "__all__"


class TaskCommentsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskComments
        fields = "__all__"

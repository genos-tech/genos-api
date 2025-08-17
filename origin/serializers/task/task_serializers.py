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


class TaskCommentsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskComments
        fields = "__all__"


class TaskCommentReactionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskCommentReactionFact
        fields = "__all__"


class TaskCommentMentionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskCommentMentionFact
        fields = "__all__"

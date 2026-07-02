from rest_framework import serializers

from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.models.task.task_models import *


class TaskMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskMaster
        fields = "__all__"


class SprintConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SprintConfig
        fields = "__all__"


class SprintSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sprint
        fields = "__all__"


class MilestoneMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneMaster
        fields = "__all__"


class MilestoneAssigneesSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneAssignees
        fields = "__all__"


class TaskAttachmentsSerializer(serializers.ModelSerializer):
    # Override the auto-derived FileField so 0-byte files are accepted.
    # Default DRF FileField rejects empty uploads with "The submitted
    # file is empty.", which surfaces in the panel as a 400 when the
    # user attaches an empty .txt placeholder — but the product wants
    # empty files to be valid attachments.
    attached_file = serializers.FileField(allow_empty_file=True)

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


class TaskBodyAttachmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskBodyAttachmentFact
        fields = "__all__"


class TaskDependencySerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskDependency
        fields = "__all__"

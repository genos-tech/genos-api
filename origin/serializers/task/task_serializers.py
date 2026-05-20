import re

from rest_framework import serializers
from origin.models.task.task_models import *
from origin.models.task.sprint_models import SprintConfig, Sprint
from origin.models.task.milestone_models import MilestoneMaster, MilestoneAssignees

# Anchored to the exact GitHub PR URL shape so we can't silently store
# issue URLs, repo roots, or anything else the frontend would fail to
# resolve via the PR detail endpoint.
_PR_URL_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+/?$")


class TaskMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskMaster
        fields = "__all__"

    def validate_linked_pr_url(self, value):
        if value in (None, ""):
            return value
        if not _PR_URL_RE.match(value):
            raise serializers.ValidationError(
                "Must be a https://github.com/<owner>/<repo>/pull/<number> URL."
            )
        return value


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

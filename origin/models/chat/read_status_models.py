from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.chat.activity_models import ActivityFact
from origin.models.common.team_models import TeamMaster


class ReadStatus(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    chat_type = models.IntegerField(blank=False, null=False)
    chat_id = models.IntegerField(blank=False, null=False)
    is_thread = models.BooleanField(blank=False, null=False)
    thread_id = models.IntegerField(blank=False, null=False)
    last_read_message_id = models.IntegerField()
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "chat_type", "chat_id", "thread_id"],
                name="unique_read_status",
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "chat_type", "chat_id", "is_thread"],
                name="read_status_user_chat_idx",
            ),
        ]


class ActivityReadStatus(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    activity = models.ForeignKey(
        ActivityFact,
        on_delete=models.SET_NULL,
        null=True,
        to_field="activity_id",
        related_name="activity_read_status",
    )
    is_read = models.BooleanField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "activity_id"],
                name="unique_activity_read_status",
            )
        ]

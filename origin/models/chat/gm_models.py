from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster
from origin.models.task.task_models import TaskMaster


class GMMaster(models.Model):
    # TODO: add team id for authorization
    gm_id = models.BigAutoField(primary_key=True, unique=True)
    group_name = models.CharField(blank=False)
    owner_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_gms",
        to_field="id",
    )
    owner_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="groups_in_team",
        to_field="team_id",
    )
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group_name", "owner_team"], name="unique_gm_master")
        ]


class GMMembers(models.Model):
    gm = models.ForeignKey(
        GMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="gm_members",
        to_field="gm_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="attending_gms",
        to_field="id",
    )
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["gm", "attendee"], name="unique_gm_member")]


class GMMessages(models.Model):
    gm = models.ForeignKey(
        GMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="gm_messages",
        to_field="gm_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_gm_messages",
        to_field="id",
    )
    message_id = models.IntegerField(blank=False, db_index=True)
    message_body = models.JSONField(blank=False)
    thread_id = models.IntegerField(blank=True, null=True)
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="gm_thread_task",
        to_field="task_id",
        blank=True,
    )
    is_deleted = models.BooleanField(default=False)
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_thread_created_at = models.DateTimeField(null=True, blank=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    uid = models.CharField(primary_key=True, max_length=255, unique=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["gm", "message_id"], name="unique_gm_message")
        ]

    def save(self, *args, **kwargs):
        """Automatically generate `uid` before saving the model."""
        self.uid = f"{self.gm.gm_id}-{self.message_id}"
        super().save(*args, **kwargs)


class GMThreadMessages(models.Model):
    gm = models.ForeignKey(
        GMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="gm_thread_messages",
        to_field="gm_id",
    )
    thread_id = models.IntegerField()
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_gm_thread_messages",
        to_field="id",
    )
    thread_message_id = models.IntegerField()
    thread_message_body = models.JSONField(blank=False)
    parent_message_uid = models.ForeignKey(
        GMMessages,
        on_delete=models.SET_NULL,
        null=True,
        related_name="thread_messages",
        to_field="uid",
    )
    is_deleted = models.BooleanField(default=False)
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["gm_id", "thread_id", "thread_message_id"], name="unique_gm_thread_message"
            )
        ]

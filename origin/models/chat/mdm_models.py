import os

from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster
from origin.models.task.task_models import TaskMaster


class MDMMaster(models.Model):
    """
    Multi-user Direct Message Master.
    Unlike GM, MDM doesn't require a formal group name and is designed for
    quick, informal conversations between 3+ members.
    """

    mdm_id = models.BigAutoField(primary_key=True, unique=True)
    owner_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_mdms",
        to_field="id",
    )
    owner_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mdms_in_team",
        to_field="team_id",
    )
    # Optional display name - if not set, will be auto-generated from member names
    display_name = models.CharField(max_length=255, blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class MDMMembers(models.Model):
    """Maps users to their MDM chats."""

    mdm = models.ForeignKey(
        MDMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mdm_members",
        to_field="mdm_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="attending_mdms",
        to_field="id",
    )
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["mdm", "attendee"], name="unique_mdm_member")
        ]


class MDMMessages(models.Model):
    mdm = models.ForeignKey(
        MDMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mdm_messages",
        to_field="mdm_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_mdm_messages",
        to_field="id",
    )
    message_id = models.IntegerField(blank=False, db_index=True)
    message_body = models.JSONField(blank=False)
    thread_id = models.IntegerField(blank=True, null=True)
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mdm_thread_task",
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
            models.UniqueConstraint(fields=["mdm", "message_id"], name="unique_mdm_message")
        ]
        indexes = [
            models.Index(
                fields=["mdm", "message_id", "is_deleted"],
                name="mdm_msg_lookup_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        """Automatically generate `uid` before saving the model."""
        self.uid = f"{self.mdm.mdm_id}-{self.message_id}"
        super().save(*args, **kwargs)


class MDMThreadMessages(models.Model):
    mdm = models.ForeignKey(
        MDMMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="mdm_thread_messages",
        to_field="mdm_id",
    )
    thread_id = models.IntegerField()
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_mdm_thread_messages",
        to_field="id",
    )
    thread_message_id = models.IntegerField()
    thread_message_body = models.JSONField(blank=False)
    parent_message_uid = models.ForeignKey(
        MDMMessages,
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
                fields=["mdm_id", "thread_id", "thread_message_id"],
                name="unique_mdm_thread_message",
            )
        ]
        indexes = [
            models.Index(
                fields=["mdm", "thread_id", "thread_message_id", "is_deleted"],
                name="mdm_thread_msg_lookup_idx",
            ),
        ]


def mdm_message_attachment_path(instance, filename):
    return os.path.join(
        "chats",
        "mdm",
        str(instance.mdm_id),
        filename,
    )


class MDMAttachmentFact(models.Model):
    mdm = models.ForeignKey(
        MDMMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="mdm_id",
    )
    uploader = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    is_thread = models.BooleanField(blank=False, null=False)
    thread_id = models.IntegerField(blank=False, null=False)
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    note_attachment_url = models.FileField(upload_to=mdm_message_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

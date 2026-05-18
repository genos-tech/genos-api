import os

from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster


class PMMessages(models.Model):
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="pm_messages",
        to_field="project_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_pm_messages",
        to_field="id",
    )
    message_id = models.IntegerField(blank=False, db_index=True)
    message_body = models.JSONField(blank=False)
    thread_id = models.IntegerField(blank=True, null=True)
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="pm_thread_task",
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
            models.UniqueConstraint(fields=["project", "message_id"], name="unique_pm_message")
        ]
        indexes = [
            models.Index(
                fields=["project", "message_id", "is_deleted"],
                name="pm_msg_lookup_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        """Automatically generate `uid` before saving the model."""
        self.uid = f"{self.project.project_id}-{self.message_id}"
        super().save(*args, **kwargs)


class PMThreadMessages(models.Model):
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="thread_messages",
        to_field="project_id",
    )
    thread_id = models.IntegerField()
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_pm_thread_messages",
        to_field="id",
    )
    thread_message_id = models.IntegerField()
    thread_message_body = models.JSONField(blank=False)
    parent_message_uid = models.ForeignKey(
        PMMessages,
        on_delete=models.SET_NULL,
        null=True,
        related_name="pm_thread_messages",
        to_field="uid",
    )
    is_deleted = models.BooleanField(default=False)
    ts_sent_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project_id", "thread_id", "thread_message_id"],
                name="unique_pm_thread_message",
            )
        ]
        indexes = [
            models.Index(
                fields=["project", "thread_id", "thread_message_id", "is_deleted"],
                name="pm_thread_msg_lookup_idx",
            ),
        ]


def project_message_attachment_path(instance, filename):
    return os.path.join(
        "chats",
        "project",
        str(instance.project_id),
        filename,
    )


class PMAttachmentFact(models.Model):
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="project_id",
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
    note_attachment_url = models.FileField(upload_to=project_message_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

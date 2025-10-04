import os

from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster


def chat_attachment_path(instance, filename):
    return os.path.join(
        "chats",
        str(instance.chat_type),
        str(instance.chat_id),
        str(instance.message_id),
        str(instance.thread_id),
        filename,
    )


class ChatAttachmentFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    chat_type = models.IntegerField(blank=False, null=False)
    chat_id = models.IntegerField(blank=False, null=False)
    message_id = models.IntegerField(blank=False, null=False)
    thread_id = models.IntegerField(blank=False, null=False)
    uploader = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    chat_attachment_url = models.FileField(upload_to=chat_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

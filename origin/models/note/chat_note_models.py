import os

from django.db import models
from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class ChatNoteMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    chat_type = models.IntegerField(blank=False, null=False)
    # v3-native routing: the chat note is keyed on the unified `Channel`
    # UUID (not the dropped legacy integer chat_id) + the thread-root
    # `Message` UUID. `channel` is SET_NULL/null so a note survives its
    # channel being deleted (notes are durable authored knowledge, not
    # chat ephemera); `thread_root_id` is a raw UUIDField (NOT a FK to
    # Message) so the note also survives the thread-root message being
    # hard-deleted. null=True on both means non-thread notes have no root
    # and the pure-UUID migration adds the columns without a backfill.
    channel = models.ForeignKey(
        Channel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_notes",
    )
    is_thread = models.BooleanField(blank=False, null=False)
    thread_root_id = models.UUIDField(null=True, blank=True)
    note_id = models.BigAutoField(primary_key=True, unique=True)
    parent_note_id = models.BigIntegerField(blank=True, null=True)
    title = models.CharField(max_length=255)
    body = models.JSONField(blank=True, null=True)
    mentioned_user_ids = models.JSONField(blank=True, default=list)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


def chat_note_attachment_path(instance, filename):
    return os.path.join(
        "notes",
        "chat",
        str(instance.note_id),
        filename,
    )


class ChatNoteAttachmentFact(models.Model):
    note = models.ForeignKey(
        ChatNoteMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="note_id",
    )
    uploader = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    chat_type = models.IntegerField(blank=False, null=False)
    channel = models.ForeignKey(
        Channel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chat_note_attachments",
    )
    is_thread = models.BooleanField(blank=False, null=False)
    thread_root_id = models.UUIDField(null=True, blank=True)
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    note_attachment_url = models.FileField(upload_to=chat_note_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

import os

from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster


class PersonalNoteMaster(models.Model):
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
    note_id = models.BigAutoField(primary_key=True, unique=True)
    parent_note_id = models.BigIntegerField(blank=True, null=True)
    title = models.CharField(max_length=255)
    body = models.JSONField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


def personal_note_attachment_path(instance, filename):
    return os.path.join(
        "notes",
        "personal",
        str(instance.note_id),
        filename,
    )


class PersonalNoteAttachmentFact(models.Model):
    note = models.ForeignKey(
        PersonalNoteMaster,
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
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    note_attachment_url = models.FileField(upload_to=personal_note_attachment_path)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

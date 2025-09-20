from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster


class NotePermissionMaster(models.Model):
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_note_permissions",
        to_field="id",
    )
    note_id = models.BigAutoField(primary_key=True, unique=True)
    # 1: Personal, 2: Task, 3: Thread
    note_type = models.IntegerField(blank=False, null=False)
    # 1: owner, 2: editor, 3: viewer
    role_id = models.IntegerField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class PersonalNoteMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_notes",
        to_field="team_id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_personal_notes",
        to_field="id",
    )
    note_id = models.BigAutoField(primary_key=True, unique=True)
    parent_note_id = models.BigIntegerField(blank=True, null=True)
    title = models.CharField(max_length=255)
    body = models.JSONField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

import os

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class PersonalNoteFolder(models.Model):
    """User-created folder for organizing personal notes in the sidebar.

    Folders are a pure organization layer, personal-only (no
    NotePermissionMaster rows, never shared, never surfaced in
    tabs/search/recents). `parent_folder_id` nests folders arbitrarily
    deep; `PersonalNoteMaster.folder_id` attaches a ROOT-level note (its
    `parent_note_id` child subtree rides along implicitly). Both are
    plain BigIntegerFields per the repo's tree convention
    (`parent_note_id` / `parent_task_id`) — folder deletion is a
    DESTRUCTIVE recursive delete of the whole subtree (folders + filed
    notes + their child notes), implemented in the view.
    """

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
    folder_id = models.BigAutoField(primary_key=True, unique=True)
    parent_folder_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    name = models.CharField(max_length=255)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


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
    # Sidebar folder membership (PersonalNoteFolder.folder_id). Only
    # meaningful on root notes (parent_note_id IS NULL) — child notes
    # follow their root. Null = top level of "My Notes".
    folder_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    title = models.CharField(max_length=255)
    body = models.JSONField(blank=True, null=True)
    # Snapshot of every user mentioned in `body` after the latest save.
    # The PUT view computes a delta against this column and emits one
    # toast per newly-mentioned user; the column itself is what
    # `loadActivityHistory` filters against so prior recipients keep
    # their feed entry on next reload. Mirrors `TaskMaster.mentioned_user_ids`.
    mentioned_user_ids = models.JSONField(blank=True, default=list)
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

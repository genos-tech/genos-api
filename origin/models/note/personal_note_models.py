import os

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class PersonalNoteFolder(models.Model):
    """User-created folder for organizing notes in the sidebar.

    `parent_folder_id` nests folders arbitrarily deep;
    `PersonalNoteMaster.folder_id` attaches a ROOT-level note (its
    `parent_note_id` child subtree rides along implicitly). Both are
    plain BigIntegerFields per the repo's tree convention
    (`parent_note_id` / `parent_task_id`).

    ONE table serves TWO sidebar spaces, told apart by `scope`:

    * ``scope="personal"`` — "My Notes". A pure organization layer:
      owner-scoped on every handler, no permission rows, never shared.
      Deletion is a DESTRUCTIVE recursive delete of the whole subtree
      (see `personal_note_folder_views.py`).
    * ``scope="team"`` — "Team Notes", the shared general space. The
      folder is the ACL CARRIER: access flows from `visibility` plus
      `NoteFolderPermission` rows down to every note filed inside, and
      `owner` means *creator*, not sole reader. Served by
      `team_note_folder_views.py`, which refuses to delete a subtree
      holding anyone else's content.

    `scope` is therefore load-bearing, not cosmetic: EVERY query on this
    table must filter it, or team folders leak into My Notes.
    """

    SCOPE_PERSONAL = "personal"
    SCOPE_TEAM = "team"

    VISIBILITY_PUBLIC = "public"
    VISIBILITY_PRIVATE = "private"

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
    # Which sidebar space this folder belongs to — see the class
    # docstring. Indexed because every list query filters on it.
    scope = models.CharField(max_length=16, default=SCOPE_PERSONAL, db_index=True)
    # Team folders only. "public" = every team member gets Editor;
    # "private" = only NoteFolderPermission grantees.
    #
    # NULL means INHERIT: resolution walks up to the nearest ancestor
    # that has an opinion. That is what makes a subfolder "accessible to
    # everyone who can reach the parent" the zero-config default — an
    # inheriting subfolder simply has no access definition of its own.
    # Always NULL for personal folders.
    visibility = models.CharField(max_length=16, blank=True, null=True)
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

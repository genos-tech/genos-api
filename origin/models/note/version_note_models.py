from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class NoteVersionMaster(models.Model):
    """One row per saved snapshot of a note (personal / task / chat).

    Designed to coalesce intra-session edits — see
    `origin.views.utils.note_version.snapshot_note_version`. A regular edit
    by the same user within the coalesce window overwrites the latest row
    in place; otherwise a new row is inserted with `version_no` bumped by
    one. Restore actions always insert a fresh row and stamp
    `restored_from_version_no` so the UI can label it as such.
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    # 1: Personal, 2: Task, 3: Chat. Mirrors NotePermissionMaster.note_type.
    note_type = models.IntegerField(blank=False, null=False)
    note_id = models.BigIntegerField(blank=False, null=False)
    # Monotonic per (note_type, note_id), starts at 1.
    version_no = models.IntegerField(blank=False, null=False)
    editor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="note_versions_authored",
        to_field="id",
    )
    title = models.CharField(max_length=255, blank=True)
    body = models.JSONField(blank=True, null=True)
    # Null for ordinary edits; set to the source version_no when this
    # row was written by a restore action.
    restored_from_version_no = models.IntegerField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["note_type", "note_id", "version_no"],
                name="unique_note_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=["note_type", "note_id", "-version_no"],
                name="noteversion_lookup_idx",
            ),
        ]

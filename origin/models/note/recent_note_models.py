from django.db import models
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class NoteRecentMaster(models.Model):
    """
    Model to track which notes a user has recently opened, across all
    note types (Personal, Task, Chat). The row is upserted on every
    "open" so `ts_opened_at` reflects the most recent open. Older rows
    beyond a fixed cap (see RecordNoteOpenView) are trimmed to keep
    the per-user history compact.
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="user_recent_notes",
        to_field="id",
    )
    note_id = models.BigIntegerField(blank=False, null=False)
    # 1: Personal, 2: Task, 3: Chat
    note_type = models.IntegerField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    # Bumped on every open via update_or_create so the table can be
    # ordered by most-recently-opened.
    ts_opened_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "note_type", "note_id"], name="unique_user_note_recent"
            )
        ]
        ordering = ["-ts_opened_at"]

    def __str__(self):
        return f"User {self.user_id} - Note {self.note_id} (Type: {self.note_type})"

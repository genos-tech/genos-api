from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder


class NoteFavoriteMaster(models.Model):
    """
    Model to track user's favorite notes across different note types.
    Users can add any note (Personal, Task, or Chat) to their favorites
    for quick access and read-later functionality.
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
        related_name="user_favorite_notes",
        to_field="id",
    )
    note_id = models.BigIntegerField(blank=False, null=False)
    # 1: Personal, 2: Task, 3: Chat
    note_type = models.IntegerField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "note_type", "note_id"], name="unique_user_note_favorite"
            )
        ]
        ordering = ["-ts_created_at"]

    def __str__(self):
        return f"User {self.user_id} - Note {self.note_id} (Type: {self.note_type})"


class NoteFolderFavorite(models.Model):
    """A note folder a user starred (MIP-747): a My Notes, Team Notes or
    cross-team shared folder. Folders are one table with one id space, so
    unlike a note favorite there is no type to record. `team` is the team
    the user starred it in — for a folder shared from another team, not
    the folder's own."""

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="favorite_note_folders",
        to_field="id",
    )
    folder = models.ForeignKey(
        PersonalNoteFolder,
        on_delete=models.CASCADE,
        related_name="favorited_by",
        to_field="folder_id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "folder"], name="unique_user_folder_favorite")
        ]
        ordering = ["-ts_created_at"]

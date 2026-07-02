from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


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

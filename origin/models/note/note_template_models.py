from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder


class NoteTemplate(models.Model):
    """A team-defined note template (MIP-1697): a name, an optional title and BlockNote blocks.

    Shared by the whole team, picked in every "New note" menu next to the built-in ones.
    `default_for_folder` makes it the template a new note in that TEAM folder starts from; at
    most one template per folder (partial unique constraint). Deleting the folder clears the
    link (SET_NULL), it never deletes the template.
    """

    template_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster, on_delete=models.SET_NULL, null=True, to_field="team_id", db_index=True
    )
    name = models.CharField(max_length=100)
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.JSONField(default=list)
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, to_field="id", related_name="+"
    )
    default_for_folder = models.ForeignKey(
        PersonalNoteFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["default_for_folder"],
                condition=models.Q(default_for_folder__isnull=False),
                name="unique_note_template_default_per_folder",
            )
        ]

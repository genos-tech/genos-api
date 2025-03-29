from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster


class ProjectMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="team_master",
        to_field="team_id",
    )
    project_id = models.BigAutoField(primary_key=True, unique=True)
    project_name = models.CharField(unique=True, blank=False)
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="own_team_master",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class ProjectMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="team_master",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_members",
        to_field="project_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="attending_projects",
        to_field="id",
    )
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    uid = models.CharField(primary_key=True, max_length=255, unique=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "attendee"], name="unique_project_member")
        ]

    def save(self, *args, **kwargs):
        """Automatically generate `uid` before saving the model."""
        self.uid = f"{self.project.project_id}-{self.attendee.id}"
        super().save(*args, **kwargs)

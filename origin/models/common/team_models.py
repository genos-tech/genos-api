from django.db import models

from origin.models.common.user_models import CustomUser


class TeamMaster(models.Model):
    team_id = models.BigAutoField(primary_key=True, unique=True)
    team_name = models.CharField(unique=True, blank=False)
    owner_email = models.EmailField(blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TeamMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="team_members",
        to_field="team_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="team_attendees",
        to_field="id",
    )
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "attendee"], name="unique_team_member")
        ]

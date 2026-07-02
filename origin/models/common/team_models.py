import os
import uuid

from django.db import models

from origin.models.common.user_models import CustomUser


def profile_image_path(instance, filename):
    return os.path.join(
        "team_profiles",
        str(instance.team_id),
        filename,
    )


class TeamMaster(models.Model):
    team_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_name = models.CharField(unique=True, blank=False)
    team_email = models.EmailField(unique=True)
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_teams",
        to_field="id",
    )
    profile_image_file = models.FileField(upload_to=profile_image_path, blank=True, null=True)
    profile_image_file_name = models.CharField(blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TeamMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_members",
        to_field="team_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_attendees",
        to_field="id",
    )
    is_deleted = models.BooleanField(default=False)
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "attendee"], name="unique_team_member")
        ]

from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster


class ProjectMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_master",
        to_field="team_id",
    )
    project_id = models.BigAutoField(primary_key=True, unique=True)
    project_name = models.CharField(unique=True, blank=False)
    project_system_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="projects",
        to_field="id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_team_master",
        to_field="id",
    )
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class ProjectMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_project_members",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="project_members",
        to_field="project_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="attending_projects",
        to_field="id",
    )
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "attendee"], name="unique_project_member")
        ]


class ProjectTags(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_tags",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="project_tags",
        to_field="project_id",
    )
    tag_id = models.IntegerField()
    tag_name = models.CharField(max_length=20)
    tag_color = models.CharField(max_length=10)
    tag_text_color = models.CharField(max_length=10)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "tag_name"], name="unique_project_tag")
        ]

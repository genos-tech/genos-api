from django.db import models
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster


class SprintConfig(models.Model):
    """Per-project sprint cadence configuration.

    Drives the auto-roll generator that materializes upcoming `Sprint`
    rows. `auto_roll=False` lets PMs author every sprint manually while
    still benefiting from a default duration when they create one.
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_sprint_configs",
        to_field="team_id",
    )
    project = models.OneToOneField(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="sprint_config",
        to_field="project_id",
    )
    duration_days = models.IntegerField(default=14)
    anchor_date = models.DateField()
    auto_roll = models.BooleanField(default=True)
    upcoming_horizon = models.IntegerField(default=6)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class Sprint(models.Model):
    """A single sprint window for a project.

    Sprint rows are either auto-generated from `SprintConfig` (and thus
    `is_auto_generated=True`) or created ad-hoc by a PM. Either kind can
    be edited (rename, shift dates) or soft-deleted.
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_sprints",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_sprints",
        to_field="project_id",
    )
    sprint_id = models.BigAutoField(primary_key=True, unique=True)
    name = models.CharField(max_length=120)
    sequence_number = models.IntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    # 'upcoming' | 'active' | 'completed' | 'archived'
    status = models.CharField(max_length=16, default="upcoming")
    is_auto_generated = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "sequence_number"],
                name="unique_project_sprint_sequence",
            )
        ]
        ordering = ["project_id", "start_date"]

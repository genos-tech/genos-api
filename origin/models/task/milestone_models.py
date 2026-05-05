from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.sprint_models import Sprint


class MilestoneMaster(models.Model):
    """Goal that groups multiple tasks within a project.

    Tasks reference a milestone via a nullable FK on `TaskMaster`.
    `sprint` is also nullable so milestones can be planned without a
    sprint or moved between sprints freely. Status reuses the task
    convention: Open | WIP | Pending | Closed | Deleted.
    """

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_milestones",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.CASCADE,
        related_name="project_milestones",
        to_field="project_id",
    )
    sprint = models.ForeignKey(
        Sprint,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sprint_milestones",
        to_field="sprint_id",
    )
    reporter = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reported_milestones",
        to_field="id",
    )
    milestone_id = models.BigAutoField(primary_key=True, unique=True)
    # Backing TaskMaster row that owns the milestone's body, comments,
    # attachments and notes. Conceptually a milestone is a task with
    # `is_milestone=True`; we keep MilestoneMaster as a side-table for
    # milestone-only metadata (sprint linkage, multi-assignees, status
    # rollups). Tasks that "live in this milestone" use
    # `parent_task_id == task.task_id` so they appear as sub-tasks in
    # the table.
    #
    # Nullable for legacy rows; the views auto-create the backing task
    # the first time a milestone without one is read or mutated.
    # String reference avoids the circular import with TaskMaster.
    task = models.ForeignKey(
        "origin.TaskMaster",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backed_milestone",
        to_field="task_id",
    )
    title = models.CharField(max_length=255)
    description = models.JSONField(blank=True, null=True)
    status = models.CharField(default="Open")
    status_code = models.BigIntegerField(blank=True, null=True)
    priority = models.CharField(blank=True, null=True)
    priority_code = models.BigIntegerField(blank=True, null=True)
    effort_level = models.CharField(blank=True, null=True)
    effort_level_code = models.BigIntegerField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    tags = models.JSONField(blank=True, null=True)
    # External links (e.g. design docs, GitHub PRs). Mirrors
    # `TaskMaster.links` shape: a list of `{ id, url, title, isGitHub }`.
    # The frontend's `DynamicURLManager` writes/reads this field via the
    # milestone PATCH endpoint, exactly like it does for tasks. The
    # backing TaskMaster row is kept in sync by `_sync_backing_task` so
    # the table view (which reads from the backing row) stays
    # consistent.
    links = models.JSONField(blank=True, null=True)
    is_deleted = models.BooleanField(default=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class MilestoneAssignees(models.Model):
    """Multi-assignee join between milestones and users."""

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_milestone_assignees",
        to_field="team_id",
    )
    milestone = models.ForeignKey(
        MilestoneMaster,
        on_delete=models.CASCADE,
        related_name="milestone_assignees",
        to_field="milestone_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="user_milestone_assignments",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["milestone", "user"],
                name="unique_milestone_assignee",
            )
        ]

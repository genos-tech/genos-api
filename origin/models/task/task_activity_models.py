from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster


class TaskActivityActionType(models.TextChoices):
    """Enumerates every kind of audit-log row recorded against a task.

    Values are stored verbatim in the DB so the frontend can switch on
    them in `TaskActivityFeed.formatActivity` without an extra
    translation table. Add new entries here in lockstep with the signal
    emitters in `origin/signals/task_signals.py`.
    """

    CREATED = "created", "Created"
    TITLE = "title_changed", "Title changed"
    STATUS = "status_changed", "Status changed"
    PRIORITY = "priority_changed", "Priority changed"
    EFFORT = "effort_changed", "Effort changed"
    ASSIGNEE = "assignee_changed", "Assignee changed"
    REPORTER = "reporter_changed", "Reporter changed"
    DUE_DATE = "due_date_changed", "Due date changed"
    DESCRIPTION = "description_edited", "Description edited"
    TAGS = "tags_changed", "Tags changed"
    PARENT = "parent_changed", "Parent changed"
    MILESTONE_LINK = "milestone_changed", "Milestone changed"
    SPRINT_LINK = "sprint_changed", "Sprint changed"
    CLOSED = "closed", "Closed"
    REOPENED = "reopened", "Reopened"
    DELETED = "deleted", "Deleted"
    ATTACHMENT_ADDED = "attachment_added", "Attachment added"
    ATTACHMENT_REMOVED = "attachment_removed", "Attachment removed"
    COMMENT_ADDED = "comment_added", "Comment added"
    COMMENT_EDITED = "comment_edited", "Comment edited"
    COMMENT_DELETED = "comment_deleted", "Comment deleted"
    # Milestones support multi-assignees via MilestoneAssignees, so we
    # log adds/removes individually rather than emitting a single
    # ASSIGNEE row with a list diff.
    MILESTONE_ASSIGNEE_ADDED = "milestone_assignee_added", "Milestone assignee added"
    MILESTONE_ASSIGNEE_REMOVED = "milestone_assignee_removed", "Milestone assignee removed"
    # GitHub PR comment surfaced on the auto-linked task (branch name
    # contains the task's display_id). Populated by `GithubWebhookView`
    # on `issue_comment` / `pull_request_review_comment` create events.
    # actor=None — GitHub commenters are not Genos users; the GitHub
    # login + avatar URL are stored in metadata.
    PR_COMMENT_ADDED = "pr_comment_added", "PR comment added"


class TaskActivity(models.Model):
    """Audit trail row recorded for every meaningful task / milestone
    change. Populated automatically by the signals in
    `origin/signals/task_signals.py`; the actor is read from the
    thread-local set by `CurrentUserMiddleware`.

    Read-only from the frontend's perspective — exposed via
    `GET /api/v2/task/activity/` and rendered by `TaskActivityFeed`.
    """

    activity_id = models.BigAutoField(primary_key=True, unique=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_task_activities",
        to_field="team_id",
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_task_activities",
        to_field="project_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="activities",
        to_field="task_id",
    )
    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acted_task_activities",
        to_field="id",
    )
    action_type = models.CharField(max_length=48, choices=TaskActivityActionType.choices)
    # `field_name` lets the frontend group/filter by attribute (e.g.
    # show only "status" changes); kept distinct from `action_type` so
    # we can have multiple action types touching the same field (e.g.
    # CLOSED vs STATUS for status).
    field_name = models.CharField(max_length=64, blank=True, null=True)
    old_value = models.JSONField(blank=True, null=True)
    new_value = models.JSONField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Most queries are "give me this task's activities, newest
        # first" — keep that path covered by an explicit index so even
        # very chatty tasks stay snappy.
        indexes = [
            models.Index(fields=["task", "-ts_created_at"], name="taskact_task_created_idx"),
        ]

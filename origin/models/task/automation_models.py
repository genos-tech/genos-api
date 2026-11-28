"""Per-project automation rules (MIP-2498): "if X happens to a task here, do Y".

`AutomationRule` is what a person configures. `AutomationRun` is both the queue and the log: a
row is written `pending` in the same transaction as the change that caused it (so a rollback takes
it with it), then moves once to `applied`, `refused` or `skipped` under a row lock. A run that
matched no rule is deleted. The logic is `origin.services.automation_engine`.
"""

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectStatus
from origin.models.task.task_models import TaskMaster

__all__ = [
    "AutomationRule",
    "AutomationRun",
    "AutomationTrigger",
    "AutomationRunState",
]


class AutomationTrigger(models.TextChoices):
    STATUS_CHANGED = "status_changed", "Status changed"
    CREATED = "created", "Created"
    TAG_ADDED = "tag_added", "Tag added"
    ASSIGNEE_CHANGED = "assignee_changed", "Assignee changed"


class AutomationRunState(models.TextChoices):
    PENDING = "pending", "Pending"
    APPLIED = "applied", "Applied"
    REFUSED = "refused", "Refused"
    SKIPPED = "skipped", "Skipped"


class AutomationRule(models.Model):
    team = models.ForeignKey(
        TeamMaster, on_delete=models.SET_NULL, null=True, related_name="team_automation_rules"
    )
    project = models.ForeignKey(
        ProjectMaster, on_delete=models.CASCADE, related_name="automation_rules"
    )
    name = models.CharField(max_length=80)
    # Rules are tried in (position, id) order; the first one that matches is the only one that acts.
    position = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)
    # "task" or "milestone" (MIP-2521). A milestone rule moves the milestone and its whole
    # sub-tree, through `milestone_manage.update_milestone`, as a move by hand does.
    applies_to = models.CharField(max_length=16, default="task")

    trigger_type = models.CharField(max_length=24, choices=AutomationTrigger.choices)
    trigger_value = models.JSONField(default=dict, blank=True)
    conditions = models.JSONField(default=list, blank=True)

    # Actions. `moves_task` says a move was configured, so a destination project that is later
    # deleted (the FK goes null) stops the rule instead of turning it into an in-place change.
    moves_task = models.BooleanField(default=False)
    destination_project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="automation_rules_into",
    )
    set_status = models.CharField(max_length=16, blank=True, default="")
    set_custom_status = models.ForeignKey(
        ProjectStatus, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    set_assignee = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # A tag NAME, looked up in the project the task ends up in.
    add_tag = models.CharField(max_length=20, blank=True, default="")

    # The rule runs as this person: whoever last saved it.
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="owned_automation_rules",
    )
    # Why the engine turned the rule off, "" while a person's own choice.
    disabled_reason = models.CharField(max_length=40, blank=True, default="")
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [
            models.Index(
                fields=["project", "position"],
                name="autorule_project_idx",
                condition=models.Q(enabled=True),
            ),
        ]


class AutomationRun(models.Model):
    task = models.ForeignKey(TaskMaster, on_delete=models.CASCADE, related_name="automation_runs")
    rule = models.ForeignKey(
        AutomationRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    source_project_id = models.BigIntegerField()
    destination_project_id = models.BigIntegerField(null=True, blank=True)
    trigger_type = models.CharField(max_length=24, choices=AutomationTrigger.choices)
    # What changed: {"status_to", "custom_status_to", "tags_added", "assignee_to"}. A milestone's
    # run hangs off its backing task and says which with "milestone_id" (and "assignees_added").
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(
        max_length=16, choices=AutomationRunState.choices, default=AutomationRunState.PENDING
    )
    reason = models.CharField(max_length=40, blank=True, default="")
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["ts_created_at"],
                name="autorun_pending_idx",
                condition=models.Q(state="pending"),
            ),
            models.Index(fields=["task", "ts_created_at"], name="autorun_task_idx"),
            models.Index(fields=["rule", "-ts_created_at"], name="autorun_rule_idx"),
        ]

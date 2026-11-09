from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster


class TaskProgressFact(models.Model):
    """One row per (task or milestone, change at or under it): the history
    behind `TaskMaster.last_progress_at` (MIP-1351).

    When a task changes, one row is written for the task itself (`depth` 0)
    and one for every ancestor above it (`depth` 1 = its parent, and so on up
    to the milestone's backing task). Reading "what happened under this
    milestone, when and by whom" is then a single indexed lookup on
    `(ancestor_task, -ts)` instead of a walk down the tree.

    The ancestors are the ones the task had AT THE TIME of the change. A task
    that later moves to another parent leaves its past with the old one; the
    move itself is recorded for the new parent (the live tree is read when the
    row is written). `origin.services.task_progress` holds the rules for what
    is written; the rows are never edited afterwards.
    """

    fact_id = models.BigAutoField(primary_key=True)
    ancestor_task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="progress_facts",
        to_field="task_id",
    )
    source_task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="+",
        to_field="task_id",
    )
    activity = models.ForeignKey(
        TaskActivity,
        on_delete=models.CASCADE,
        related_name="progress_facts",
    )
    actor = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        to_field="id",
    )
    # Copied from the activity so a reader never has to join for it.
    action_type = models.CharField(max_length=48)
    # 0 = the task's own change, 1 = a direct child's, 2 = a grandchild's...
    depth = models.PositiveSmallIntegerField(default=0)
    ts = models.DateTimeField()

    class Meta:
        constraints = [
            # One row per activity per ancestor, which is what makes the
            # backfill safe to run twice.
            models.UniqueConstraint(
                fields=["activity", "ancestor_task"],
                name="taskprogressfact_activity_ancestor_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["ancestor_task", "-ts"], name="taskprogress_anc_ts_idx"),
        ]

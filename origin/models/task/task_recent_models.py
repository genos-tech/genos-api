from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster


class TaskRecent(models.Model):
    """A task a user opened, for the task sidebar's Recents (MIP-1063).

    Private to the user: one row per (user, task), and `ts_opened_at` is moved
    to now each time they open it again. It holds a timestamp only, no time
    spent. `team` is the team that owns the task's project, which scopes the
    trim; who may read a row is decided by project access when the list is read,
    never by this column.
    """

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="recent_tasks",
        to_field="id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="recent_opens",
        to_field="task_id",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_task_recents",
        to_field="team_id",
    )
    # Not `auto_now`: the open is stamped explicitly, and the one-off backfill
    # writes the time of the user's last edit.
    ts_opened_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "task"], name="unique_user_task_recent")
        ]
        indexes = [
            models.Index(
                fields=["user", "team", "-ts_opened_at"], name="taskrecent_user_team_opened"
            )
        ]
        ordering = ["-ts_opened_at"]

    def __str__(self):
        return f"User {self.user_id} - Task {self.task_id}"

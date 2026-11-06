"""Tasks a user opened (MIP-1063): record an open, and read the list back.

`record_open` is called by the client when a task is shown to the user. Reads by
the agent or MCP never come here, so only the person looking at a task counts.
"""

from django.db import transaction
from django.utils import timezone

from origin.models.task.task_models import TaskMaster
from origin.models.task.task_recent_models import TaskRecent
from origin.views.utils.scope_guards import is_project_member, reachable_project_ids

# Rows kept per (user, owning team). 20 per project in the list times about 15
# projects; older ones are trimmed on every open.
RECENT_TASKS_CAP = 300
# The sidebar shows at most this many per project.
RECENT_TASKS_PER_PROJECT = 20


def can_open(user_id, task: TaskMaster) -> bool:
    """Same door as reading the task: a live task of a project the caller is in."""
    if task.is_deleted or task.is_init_task or task.project_id is None:
        return False
    return is_project_member(task.project_id, user_id)


def record_open(*, user_id, task: TaskMaster):
    """Move the task to the top of the user's recents. Returns the row."""
    team_id = task.project.team_id if task.project_id else task.team_id
    now = timezone.now()
    with transaction.atomic():
        row, created = TaskRecent.objects.get_or_create(
            user_id=user_id, task=task, defaults={"team_id": team_id, "ts_opened_at": now}
        )
        if not created:
            row.team_id = team_id
            row.ts_opened_at = now
            row.save(update_fields=["team", "ts_opened_at"])
        keep = list(
            TaskRecent.objects.filter(user_id=user_id, team_id=team_id)
            .order_by("-ts_opened_at", "-id")
            .values_list("id", flat=True)[:RECENT_TASKS_CAP]
        )
        TaskRecent.objects.filter(user_id=user_id, team_id=team_id).exclude(id__in=keep).delete()
    return row


def visible_recents(user_id, team_id, statuses) -> list[TaskRecent]:
    """The user's opened tasks they can still open from `team_id`'s workspace,
    newest open first, at most RECENT_TASKS_PER_PROJECT per project.

    Access is decided here, on every read: a project the user left, or deleted,
    drops out whatever the row says.
    """
    reachable = reachable_project_ids(user_id, team_id)
    if not reachable:
        return []
    rows = (
        TaskRecent.objects.filter(
            user_id=user_id,
            task__project__in=reachable,
            task__project__is_deleted=False,
            task__status__in=statuses,
            task__is_init_task=False,
            task__is_deleted=False,
        )
        .select_related("task", "task__project")
        .order_by("-ts_opened_at", "-id")
    )
    kept: dict[int, int] = {}
    out = []
    for row in rows:
        pid = row.task.project_id
        if kept.get(pid, 0) >= RECENT_TASKS_PER_PROJECT:
            continue
        kept[pid] = kept.get(pid, 0) + 1
        out.append(row)
    return out

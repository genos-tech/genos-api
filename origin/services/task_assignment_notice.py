"""Tell a person that someone assigned them an existing task (MIP-1461).

Creating a task tells its assignee because the "task created" card `@`-mentions them
(`task_card_post` for a server write, the browser's send for the app). Handing someone a
task that already exists told nobody: the app only rewrote the card, which raises no
activity, and `assign_task` just saved a column. So the new assignee found out by
happening to open the task. Ken decided (2026-11-13) that they should be told.

`notify_task_assigned` is that one rule, called from every place that reassigns a task:
the app's task save (`TaskMasterView.put`), `assign_task`, and `update_tasks_bulk`. It
writes the row `v3_activity.create_task_assign_activity` describes, then does what any
activity gets: Web Push (and email) and the live `activity.created` to the open app.

Who is NOT told: whoever made the change (assigning yourself), the person being unassigned,
a milestone's backing task (its people are `MilestoneAssignees`, a separate rule), and a
task whose assignee did not actually change.

A task with no card in its project chat (a sub-task, one that predates cards) is given one
first, which `@`-mentions the new assignee: that mention IS the notification, and it gives
the task a place in the chat for the notification to open.

Best effort, like every observer of a write: the assignee is already saved, so a failure is
logged and costs the notice, never the change.
"""

from __future__ import annotations

import logging

from origin.models.chat.unified_models import Activity, Message
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster
from origin.services import realtime_push, v3_activity
from origin.services.task_card_post import _pm_channel, post_task_created_card
from origin.services.webpush_dispatch import schedule_push_for_activities

log = logging.getLogger(__name__)


def notify_task_assigned(task, *, actor_id, previous_assignee_id) -> list[Activity]:
    """Tell `task`'s new assignee, if it has a different one than `previous_assignee_id`.

    `task` is the saved `TaskMaster`; it is read again, so a stale instance is fine.
    `actor_id` is who made the change. Returns the activities written (for tests).
    Never raises.
    """
    try:
        task = (
            TaskMaster.objects.select_related("project", "assignee", "reporter")
            .filter(task_id=task.task_id)
            .first()
        )
        if task is None or task.project_id is None or task.is_milestone:
            return []
        new_id = str(task.assignee_id) if task.assignee_id else None
        if new_id is None or new_id == str(previous_assignee_id or "") or new_id == str(actor_id):
            return []
        channel = _pm_channel(task.project_id)
        if channel is None:
            return []
        card = (
            Message.objects.select_related("channel", "sender")
            .filter(channel=channel, task_id=task.task_id, is_thread_reply=False)
            .filter(deleted_at__isnull=True)
            .order_by("seq")
            .first()
        )
        if card is None:
            post_task_created_card(task, actor_id=actor_id)
            return []
        actor = CustomUser.objects.filter(id=actor_id).first()
        if actor is None:
            return []
        activities = v3_activity.create_task_assign_activity(
            message=card, recipient_id=new_id, actor=actor
        )
        schedule_push_for_activities(activities)
        for activity in activities:
            realtime_push.push_activity(activity)
        return activities
    except Exception:  # noqa: BLE001 — the assignee is saved; an observer must not fail the write
        log.warning(
            "task assignment notice failed task=%s", getattr(task, "task_id", None), exc_info=True
        )
        return []

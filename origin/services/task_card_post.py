"""Post the "task created" card into the project's chat when no browser will (MIP-1450).

In the app, creating a task is two steps and the SECOND is the browser's: the task
is saved (`PUT /task/`), then the client sends the card (`uploadNewTask.ts`,
`sendMilestoneCreatedMessage.ts`) into the project's PM channel through the same
`POST /api/v3/channels/{id}/messages/` any message uses. That one message is what
puts the task in the project chat AND what tells the assignee: its body `@`-mentions
them, and the send raises their mention activity, Web Push and email.

A task made over MCP or by the in-app agent has no browser, and nothing here posted
the card. So the task was missing from the project chat and its assignee was never
told (only an `@` chip in the task BODY pinged anyone, `notify_task_body_mentions`).
This is the missing second step, run after the task exists, by the write paths that
have no client:

  * `create_task`, `create_task_plan` and `manage_milestone` (create): the agent's
    tools, which MCP serves too;
  * `POST /api/public/v1/tasks/`, the public REST API (MIP-1462).

It must NOT run for a task made in the app: its client still posts its own card, and
a second would double-post. That is why this is called from the no-client paths by
name rather than from a model signal that would fire for both. (An importer on the
public REST API posts one card per task; that is accepted for now and may change with
the feature.)

It goes through `message_views._allocate_seq_and_create_message_with_activities`, the
function the REST send uses, so the card is the same row with the same fan-out as the
app's: the per-channel `seq` lock, the `MessageMention` rows, the mention activity, the
Web Push, the outbound `message.created` webhook. The activities are then announced to
the recipients' open apps (`realtime_push`), which the socket handler does for a message
sent from the app. One difference, on purpose: no sidebar row for a task assigned to its
own maker (`self_assign=False`). The app adds one so a person who clicked Create has a
trace; here MIP-1432's single notice per call already tells them, and a plan assigns
every task to its maker.

The card is also announced to the project's chat room (`realtime_push.push_message`, MIP-1459),
so a colleague with that chat already open sees it at once. That needs genos-sockets'
`/internal/broadcast/message`; until it is deployed the push is refused and logged, and the card
shows at the next sync as before.

Best effort, like every other observer of a write: the task is already saved, so a
failure here is logged and costs the card, never the create. Idempotent per task: a
task that already has a card is left alone.
"""

from __future__ import annotations

import logging

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.user_models import CustomUser
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import realtime_push, task_card

log = logging.getLogger(__name__)


def _pm_channel(project_id) -> Channel | None:
    """The project's PM channel, found the way the task-comment mirror finds it, so a
    card and the comments threaded under it can never end up in different channels."""
    return Channel.objects.filter(kind=ChannelKind.PM, legacy_chat_id=int(project_id)).first()


def _post(*, project, task_id, actor_id, body, body_text, metadata) -> Message | None:
    from origin.views.chat.message_views import (  # noqa: PLC0415
        _allocate_seq_and_create_message_with_activities,
    )

    channel = _pm_channel(project.project_id)
    if channel is None:
        log.warning("task card: project %s has no PM channel", project.project_id)
        return None
    # A card exists already (a retry, or a comment's mirror that made a bare header first):
    # one card per task, as the app keeps it.
    if Message.objects.filter(channel=channel, task_id=task_id, is_thread_reply=False).exists():
        return None
    actor = CustomUser.objects.filter(id=actor_id).first()
    if actor is None:
        return None
    message, activities = _allocate_seq_and_create_message_with_activities(
        channel=channel,
        sender=actor,
        body=body,
        body_text=body_text,
        parent_id=None,
        metadata=metadata,
        # Whoever made the task through an AI app already gets MIP-1432's one notice for the
        # call; a row per self-assigned task would bury it (a plan assigns every task to its maker).
        self_assign=False,
    )
    # The socket handler announces the message to the chat room and each activity to its
    # recipient when the app sends a message; here there is no handler, so tell the open apps
    # ourselves: the card to everyone with the chat open, the activity to the person it is for.
    realtime_push.push_message(message, realtime_push.CREATED)
    for activity in activities:
        realtime_push.push_activity(activity)
    return message


def _system_user_id(project) -> str | None:
    return str(project.project_system_user_id) if project.project_system_user_id else None


def post_task_created_card(task, *, actor_id) -> Message | None:
    """Post `task`'s card into its project's PM channel, as the person who made it.

    `task` is a saved `TaskMaster`; `actor_id` is who made it (the card's sender, and the
    one person the activity fan-out leaves out). A task with no project has no chat to post
    to. Never raises.

    The row is read again rather than trusted: a tool's instance still holds what the caller
    typed (`due_date` is the raw string `2026-6-30`, not a date), and the card must show what
    the row stores.
    """
    try:
        task = (
            TaskMaster.objects.select_related("project", "assignee", "reporter")
            .filter(task_id=task.task_id)
            .first()
        )
        if task is None or task.project_id is None:
            return None
        project = task.project
        body = task_card.task_card_body(
            title=task.title,
            status=task.status or "Open",
            priority=task.priority,
            assignee=task.assignee,
            reporter=task.reporter,
            due_date=task.due_date,
            team_id=task.team_id,
            effort=task.effort_level or None,
        )
        return _post(
            project=project,
            task_id=task.task_id,
            actor_id=actor_id,
            body=body,
            # The app sends the bare title as the preview text, not the emoji-prefixed heading.
            body_text=task.title,
            metadata={
                "taskId": task.task_id,
                "displayId": task.display_id,
                "taskStatus": task.status,
                "systemUserId": _system_user_id(project),
            },
        )
    except Exception:  # noqa: BLE001 — the task is saved; an observer must not fail the write
        log.warning("task card post failed task=%s", getattr(task, "task_id", None), exc_info=True)
        return None


def post_milestone_created_card(milestone, *, actor_id) -> Message | None:
    """Post a new milestone's card (sprint, status, priority, reporter, assignees, due).

    The card hangs off the milestone's backing task, as the app's does. Never raises.
    """
    try:
        milestone = (
            MilestoneMaster.objects.select_related("project", "sprint", "reporter")
            .filter(milestone_id=milestone.milestone_id)
            .first()
        )
        if milestone is None or milestone.task_id is None:
            return None
        project = milestone.project
        assignees = [
            row.user
            for row in MilestoneAssignees.objects.filter(milestone=milestone)
            .select_related("user")
            .order_by("ts_created_at", "pk")
        ]
        body = task_card.milestone_card_body(
            title=milestone.title,
            status=milestone.status,
            priority=milestone.priority,
            sprint_name=milestone.sprint.name if milestone.sprint_id else None,
            reporter=milestone.reporter,
            assignees=assignees,
            due_date=milestone.due_date,
            team_id=milestone.team_id,
        )
        return _post(
            project=project,
            task_id=milestone.task_id,
            actor_id=actor_id,
            body=body,
            body_text=milestone.title,
            metadata={
                "taskId": milestone.task_id,
                "taskStatus": milestone.status,
                "systemUserId": _system_user_id(project),
            },
        )
    except Exception:  # noqa: BLE001 — the milestone is saved; an observer must not fail the write
        log.warning(
            "milestone card post failed milestone=%s",
            getattr(milestone, "milestone_id", None),
            exc_info=True,
        )
        return None

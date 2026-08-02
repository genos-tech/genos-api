"""Everything that has to happen *after* a task comment row is saved.

A `TaskComments` insert is only the first half of adding a comment. The
second half — the v3 thread mirror, the mention and participant
activities, the web push — is what makes anyone find out about it, and
it used to live inline in `TaskCommentsView.post` alone. The agent's
`add_comment` tool wrote the row and stopped, so a comment it added was
invisible in the PM task thread and notified nobody: the assignee only
learned about it by opening the task. Extracting the block here is what
lets both callers share one definition of "a comment was added" instead
of one of them having a quieter version of it.

Every step is best-effort and independent. A comment that is already
committed must never be undone by an observer failing, so each stage
catches its own exceptions and the caller always gets a result. The
order is load-bearing in one place, noted below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import mention_extractor, unified_writer, v3_activity, webhook_enqueue
from origin.services.webpush_dispatch import schedule_push_for_activities
from origin.views.utils.mention_handler import resolve_group_members

logger = logging.getLogger(__name__)


@dataclass
class CommentFanout:
    """What the callers need back.

    `activities` is the model rows (the REST view serialises them onto
    the response so the Flask handler can broadcast `activity.created`);
    `mentioned_user_ids` feeds the non-member-mention report.
    """

    activities: list[Any] = field(default_factory=list)
    mentioned_user_ids: list[str] = field(default_factory=list)


def _schedule_webhook(comment: TaskComments, task_id: int) -> None:
    """Outbound webhook. Runs BEFORE the mirror so a mirror failure
    cannot cost the integrator the event, and reads the task fresh rather
    than trusting the caller's payload, since it names the project the
    subscription filters on."""
    try:
        task = (
            TaskMaster.objects.filter(task_id=task_id)
            .only("task_id", "team_id", "project_id", "project_task_number")
            .select_related("project")
            .first()
        )
        if task is not None:
            webhook_enqueue.schedule_comment_event(comment, task)
    except Exception:  # noqa: BLE001 — an observer must not break the write
        logger.warning("webhook comment enqueue failed", exc_info=True)


def _mentioned_ids(comment_body: Any) -> set[str]:
    body = comment_body or []
    mentioned = set(mention_extractor.extract_mentioned_user_ids(body))
    group_ids = mention_extractor.extract_mention_group_ids(body)
    if group_ids:
        mentioned |= resolve_group_members(group_ids)
    return mentioned


def _participant_ids(task_id: int, mentioned: set[str]) -> set[str]:
    """Who to ping for a comment that @-mentioned them or not.

    A comment with no @mention otherwise pings nobody, so notify the
    task's assignee, its collaborators, and everyone who has previously
    commented. The commenter is skipped inside the activity helper.
    """
    participants: set[str] = set()

    assignee_id = (
        TaskMaster.objects.filter(task_id=task_id).values_list("assignee_id", flat=True).first()
    )
    if assignee_id is not None:
        participants.add(str(assignee_id))

    # Collaborators are notified on task activity exactly like the
    # assignee — add them to the participant set.
    for collab_id in (
        TaskMaster.objects.filter(task_id=task_id)
        .values_list("collaborators__id", flat=True)
        .distinct()
    ):
        if collab_id is not None:
            participants.add(str(collab_id))

    for sender_id in (
        TaskComments.objects.filter(task=task_id, is_deleted=False)
        .values_list("sender_id", flat=True)
        .distinct()
    ):
        if sender_id is not None:
            participants.add(str(sender_id))

    # Excluding @-mentioned users AFTER adding collaborators so a
    # mentioned collaborator gets the MENTION activity (more specific),
    # never a duplicate THREAD_REPLY.
    return participants - mentioned


def fan_out_task_comment(comment: TaskComments) -> CommentFanout:
    """Mirror a saved comment into v3 and notify everyone who cares.

    `comment` must already be persisted — this reads its `task_id`,
    `comment_id`, `sender_id` and `comment_body` and never writes to it.
    Never raises.
    """
    result = CommentFanout()
    task_id = int(comment.task_id)
    sender_id = str(comment.sender_id) if comment.sender_id else None

    _schedule_webhook(comment, task_id)

    # Mirror the comment as a v3 thread-reply Message under the PM task
    # header, so PM task threads render comments through the unified
    # message path instead of a parallel comments-only endpoint.
    #
    # `bypass_flag=True`: task comments live in the legacy `TaskComments`
    # table and the v3 PM task thread renders them ONLY through this
    # mirror. With the legacy chat tables dropped, v3 is the sole chat
    # backend, so the mirror must run unconditionally — not gated on the
    # (now-vestigial) `UNIFIED_MESSAGING_DUAL_WRITE` flag, which would
    # otherwise leave live comments invisible in the PM thread and
    # produce no comment-mention activity.
    try:
        mirror = unified_writer.write_task_comment_as_thread_reply(
            task_id=task_id,
            comment_id=comment.comment_id,
            sender_id=sender_id,
            body=comment.comment_body,
            bypass_flag=True,
        )
    except Exception:  # noqa: BLE001 — the drift cron catches divergence
        logger.warning("task-comment v3 mirror failed", exc_info=True)
        return result

    if mirror is None or mirror.sender is None:
        return result

    # A failure building activities must NOT surface to a caller whose
    # comment is already saved. On error we skip the live broadcast — the
    # activity feed reconciles on next load.
    try:
        mentioned = _mentioned_ids(comment.comment_body)
        result.mentioned_user_ids = list(mentioned)

        # `skip_actor=False`: tagging yourself in a comment still pings,
        # consistent with task-body and note mentions.
        mention_acts = v3_activity.create_mention_activities(
            message=mirror,
            mentioned_user_ids=result.mentioned_user_ids,
            actor=mirror.sender,
            skip_actor=False,
        )
        participant_acts = v3_activity.create_comment_participant_activities(
            message=mirror,
            recipient_ids=_participant_ids(task_id, {str(u) for u in mentioned if u}),
            actor=mirror.sender,
        )
        result.activities = list(mention_acts) + list(participant_acts)

        # Web Push for away recipients: @mention rows route to the
        # mention category, the plain participant fan-out (THREAD_REPLY
        # on the mirror, which carries metadata.taskCommentId) to the
        # task_comments category.
        schedule_push_for_activities(result.activities)
    except Exception as exc:  # noqa: BLE001 — never break the saved comment
        logger.warning("task-comment activity fan-out failed: %s", exc)
        result.activities = []

    return result

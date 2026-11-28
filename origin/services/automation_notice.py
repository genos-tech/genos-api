"""What people are told when a project rule moves a task (MIP-2500).

The task takes its card and thread to the new project's chat (`task_card_move`), so that chat
needs nothing more. Two things are added:

THE OLD CHAT gets one line in its main timeline, or the card would just be gone.
  * Author: nobody (`sender` is null, as `task_close_notice`'s line), with
    `metadata.systemNotice = "task_moved_by_rule"`. A member's own message has a sender, so nobody
    can forge one.
  * PRIVACY. A rule may move a task into a project FEWER people can open, and the line is one
    stored row the whole old chat reads. So neither the new project's name nor the task's title or
    display id is ever written into it: the metadata holds their ids only
    (`movedTaskId`, `destinationProjectId`), and the app resolves each per viewer through the
    ordinary access-checked reads ("another project" / "a task" for who cannot open them). The
    stored text names the rule alone, which is the OLD project's own object.
  * No `Message.task` and no `metadata.taskId`: those mark a task CARD.
  * No `@`-mention, no activity row, no push: it must not ring anybody.

THE ASSIGNEE is told on the card, by the row an assignment of an existing task writes, flagged
`ruleMove` so the push does not claim somebody assigned it. A rule that also changed the assignee
tells the new one the ordinary way (`notify_task_assigned`) and this is not called.

Best effort, like every observer of a write: a failure is logged and never costs the move.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.chat.unified_models import Message
from origin.services import realtime_push
from origin.services.task_card_post import _pm_channel

log = logging.getLogger(__name__)

NOTICE_KIND = "task_moved_by_rule"


def notice_text(rule_name: str, what: str = "task") -> str:
    return f'Rule "{rule_name}" moved a {what} to another project.'


def post_move_notice(
    *, task_id, rule_name: str, source_project_id, destination_project_id, milestone: bool = False
) -> Message | None:
    """The line in the old project's chat. None when it has no chat. Never raises."""
    try:
        channel = _pm_channel(source_project_id)
        if channel is None or channel.is_deleted:
            return None
        from origin.views.chat.message_views import _allocate_seq_and_create_message  # noqa: PLC0415

        text = notice_text(rule_name, "milestone" if milestone else "task")
        # A savepoint: a failure rolls back the line alone and leaves the move standing.
        with transaction.atomic():
            message = _allocate_seq_and_create_message(
                channel=channel,
                sender=None,
                body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
                body_text=text,
                parent_id=None,
                metadata={
                    "systemNotice": NOTICE_KIND,
                    "movedTaskId": task_id,
                    "destinationProjectId": destination_project_id,
                    "ruleName": rule_name,
                    # `movedTaskId` is then the milestone's backing task (MIP-2521).
                    **({"movedMilestone": True} if milestone else {}),
                },
            )
        # Sent on commit, after the card's removal from this chat (queued first by the move).
        realtime_push.push_message(message, realtime_push.CREATED)
        return message
    except Exception:  # noqa: BLE001 — the move stands; the notice is an observer of it
        log.warning("rule move notice failed task=%s", task_id, exc_info=True)
        return None

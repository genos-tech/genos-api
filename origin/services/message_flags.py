"""Flag a message for later, mark it done, remove the flag: the rules the REST view and the agent's
tool share (MIP-1562).

`FlagView` (`/api/v3/messages/<id>/flag/`) is what the app's "Flag for later", "Mark done" and
"Remove flag" call, through the sockets events `flag.add`, `flag.complete` and `flag.remove`.
The flag is the caller's own bookmark: nobody else is told, and the sockets events only keep the
caller's other open tabs in step.

A task comment is flagged through its mirror message (the thread reply under the project chat's
task card), so the people who can flag one are the members of that chat: a task's assignee or
reporter who is not on the project can open the task and cannot flag its comments, and the app
does not offer them the item (`TaskCommentList`, MIP-1442 / MIP-1480). `comment_mirror` is that
rule for a writer that has no list to read it off.
"""

from __future__ import annotations

from django.utils import timezone

from origin.models.chat.unified_models import Flag, Message
from origin.services import message_reminders, unified_writer
from origin.services.channel_scope import is_chat_member

FLAG = "flag"
UNFLAG = "unflag"
DONE = "done"
ACTIONS = (FLAG, UNFLAG, DONE)


def comment_mirror(task_id, comment_id, user_id) -> Message | None:
    """The mirror message of a task comment, when `user_id` may flag it; else None.

    None for a comment that has no mirror yet (`repair_task_comment_mirrors` writes it) and for a
    person who is not a member of the chat the mirror lives in: both read as "nothing to flag".
    """
    message = (
        Message.objects.filter(id=unified_writer.task_comment_message_uuid(task_id, comment_id))
        .select_related("channel")
        .first()
    )
    if message is None or not is_chat_member(message.channel_id, user_id):
        return None
    return message


def flag_message(user, message) -> tuple[Flag, bool]:
    """Flag `message` for `user`. `(flag, created)`; re-flagging a done flag reopens it in place
    (it re-enters the active list instead of silently staying done)."""
    flag, created = Flag.objects.get_or_create(user=user, message=message)
    if not created and flag.completed_at is not None:
        flag.completed_at = None
        flag.save(update_fields=["completed_at"])
    return flag, created


def complete_flag(user, message) -> Flag | None:
    """Mark `user`'s flag on `message` done, or None when there is none. Done retires the pending
    reminder: its job was to bring the message back, and the person is finished with it."""
    flag = Flag.objects.filter(user=user, message=message).first()
    if flag is None:
        return None
    flag.completed_at = timezone.now()
    flag.save(update_fields=["completed_at"])
    message_reminders.cancel_pending(user=user, message=message)
    return flag


def unflag_message(user, message) -> int:
    """Remove `user`'s flag on `message`: how many rows went (0 when it was not flagged). Removing
    the flag is the other way of being done with it, so the pending reminder goes too."""
    removed, _ = Flag.objects.filter(user=user, message=message).delete()
    message_reminders.cancel_pending(user=user, message=message)
    return removed

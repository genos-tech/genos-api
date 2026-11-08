"""Chat-thread → task linkage invariants.

A chat thread (DM/GM/MDM) may be the origin of at most ONE task — the
frontend hides its "Create task" action once a thread has a task, but
that gate is client state and races (stale menus, two members creating
at once, old sessions). These helpers give the create paths — the task
finalize PUT and the milestone POST (whose backing TaskMaster row also
carries the linkage) — one shared server-side check.

`chat_id` / `thread_id` are the v3 Channel / Message UUIDs stored as
opaque CharFields on TaskMaster (legacy rows hold ints); comparison is
by exact string match, mirroring `GetTaskByThreadIdView`.

A link may only name a chat the writer can read (`thread_link_is_unreadable`, MIP-1314). The
ids come from the request body, so without it a project member could attach a task to the
thread of a chat they are not in: that thread then showed a task nobody there put on it, and
the one-task-per-thread rule kept the real task from ever being made from it.
"""

import uuid

from origin.models.chat.unified_models import Channel, Message
from origin.models.task.task_models import TaskMaster
from origin.services.channel_scope import can_read_chat

# The one refusal text of every writer, so none of them can tell a caller why.
THREAD_NOT_FOUND = "Thread not found."


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def thread_link_is_unreadable(user_id, team_id, chat_id, thread_id) -> bool:
    """True when `user_id` may not link a task of `team_id` to this chat / thread.

    Only ids that name a v3 chat or message are judged. No ids, or the numbers and `-1` that
    rows from before chat v3 hold, join to nothing (lines and the by-thread lookup read UUIDs
    only), so they stay accepted as they were. Anything that does name a chat must be one the
    writer can read in the task's team (`can_read_chat`: member, or on the project for a
    project chat, and the chat is in the team's list), and a thread must sit in that chat. A
    chat or message that does not exist counts as unreadable, so the three cases a caller
    could tell apart (missing, someone else's, another team's) have one answer.
    """
    chat_uuid = _uuid_or_none(chat_id)
    thread_uuid = _uuid_or_none(thread_id)
    if chat_uuid is None and thread_uuid is None:
        return False
    channel = None
    if chat_uuid is not None:
        channel = Channel.objects.filter(id=chat_uuid).first()
        if channel is None:
            return True
    if thread_uuid is not None:
        message = Message.objects.filter(id=thread_uuid).select_related("channel").first()
        if message is None:
            return True
        if channel is None:
            channel = message.channel
        elif message.channel_id != channel.id:
            return True
    return not can_read_chat(channel, team_id, user_id)


def find_thread_link_conflict(
    team_id,
    chat_id,
    thread_id,
    *,
    exclude_task_id=None,
):
    """Return the task_id of a live task already linked to this thread,
    or None. `exclude_task_id` skips the row being written (an update
    re-sending its own linkage is not a conflict)."""

    if not chat_id or not thread_id:
        return None
    qs = TaskMaster.objects.filter(
        team_id=team_id,
        chat_id=str(chat_id),
        thread_id=str(thread_id),
        is_init_task=False,
        is_deleted=False,
    )
    if exclude_task_id is not None:
        qs = qs.exclude(task_id=exclude_task_id)
    return qs.values_list("task_id", flat=True).first()

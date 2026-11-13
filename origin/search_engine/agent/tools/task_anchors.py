"""Where a task's talk and notes live: the `anchors` of `fetch_task` (MIP-1288).

`fetch_task` returns the task and its comments but neither the chat thread it was made from
nor its task note, so an agent that starts from a task could not get to the conversation behind
it. Behind `SEARCH_ENGINE["AGENT_FETCH_TASK_ANCHORS"]` (default off) the result gains

    "anchors": [{"anchor_type": "thread", "anchor_id": "<root message id>"},
                {"anchor_type": "chat",   "anchor_id": "<channel id>"},
                {"anchor_type": "note",   "anchor_id": "task:<note id>"}]

in the SAME words the timeline tool takes (MIP-1281: `anchor_type` task | thread | chat | note,
a note as `<family>:<id>`), in that order, notes by id. Off, nothing is added: not even an empty
list, so the result is byte-for-byte what it was.

WHAT AN ANCHOR IS. A pointer, never content: no title, no count, no text. The model follows it
with the tools it has (`fetch_chat_thread` with the chat as `chat_id` and the thread as
`thread_id`; `fetch_note` with `note_type: "task"` and the number after `task:` as `note_id`).

ACCESS. An anchor is listed only when the caller can open what it points at, by the rule of the
tool that opens it, so that the next call is never refused and the list says nothing about what
the caller cannot see (a list with nothing in it reads the same for "none" and "none you may
open"):

  * thread / chat: `channel_scope.can_read_chat` on the channel, the rule `fetch_chat_thread`
    applies. The channel is the one the thread's ROOT MESSAGE is in, not the task's `chat_id`
    column (informational only, per its model comment). A chat anchor needs a chat the task
    names AND the caller can read.
  * thread, besides: a live root message (a top-level one: a reply id is not a thread), not in
    a project chat (those threads are task comments, and the timeline refuses them), and ONE
    live task of the team claiming it. A thread two live tasks claim is attached to neither,
    the rule the task timeline uses (`item_lines._claims`; a test pins the two equal).
  * note: the task's own note(s) in this team, each one `fetch_note` opens (the task-note ACL
    set) AND `may_read_note` (the rule `update_note` and the other note tools use) allows.

A chat anchor is kept when the thread is dropped for being claimed twice (the chat is the
task's own stored key; the claim rule is about which task a THREAD's messages belong to) and
the other way round never happens (a thread needs a readable chat).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import may_read_note, task_note_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.chunkers.base import NOTE_TYPE_TASK
from origin.services.channel_scope import can_read_chat

FLAG = "AGENT_FETCH_TASK_ANCHORS"


def enabled() -> bool:
    """Read at call time, so an eval arm or `override_settings` can flip it."""
    return bool(settings.SEARCH_ENGINE.get(FLAG))


def _uuid_text(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _sole_claimant(team_id: str, thread_id: str, task_id: int) -> bool:
    """True when `task_id` is the one live task of the team that claims `thread_id`.

    The rule of `item_lines._claims` (same team, not deleted, not an init task), written as one
    query for one thread; `test_task_anchors` pins it equal to that function."""
    claimants = list(
        TaskMaster.objects.filter(
            team_id=team_id, thread_id=thread_id, is_deleted=False, is_init_task=False
        ).values_list("task_id", flat=True)[:2]
    )
    return claimants == [task_id]


def _readable_channel(raw: Any, ctx: ToolContext) -> Channel | None:
    channel_id = _uuid_text(raw)
    if channel_id is None:
        return None
    channel = Channel.objects.filter(id=channel_id, is_deleted=False).first()
    if channel is None or not can_read_chat(channel, ctx.team_id, ctx.user_id):
        return None
    return channel


def _thread_anchor(task: TaskMaster, ctx: ToolContext) -> dict[str, str] | None:
    thread_id = _uuid_text(task.thread_id)
    if thread_id is None:
        return None
    root = (
        Message.objects.filter(id=thread_id, is_thread_reply=False, deleted_at__isnull=True)
        .select_related("channel")
        .first()
    )
    if root is None or root.channel.is_deleted or root.channel.kind == ChannelKind.PM:
        return None
    if not can_read_chat(root.channel, ctx.team_id, ctx.user_id):
        return None
    if not _sole_claimant(str(ctx.team_id), thread_id, task.task_id):
        return None
    return {"anchor_type": "thread", "anchor_id": thread_id}


def _chat_anchor(task: TaskMaster, ctx: ToolContext) -> dict[str, str] | None:
    channel = _readable_channel(task.chat_id, ctx)
    if channel is None:
        return None
    return {"anchor_type": "chat", "anchor_id": str(channel.id)}


def _note_anchors(task: TaskMaster, ctx: ToolContext) -> list[dict[str, str]]:
    out = []
    notes = TaskNoteMaster.objects.filter(task_id=task.task_id, team_id=ctx.team_id).order_by(
        "note_id"
    )
    for note in notes:
        if str(note.team_id or "") != str(ctx.team_id):
            continue
        # What `fetch_note` opens ...
        allowed = task_note_acl_user_ids(
            owner_id=note.owner_id, project_id=note.project_id, note_id=note.note_id
        )
        if str(ctx.user_id) not in allowed:
            continue
        # ... and what the note tools after it (`update_note`, `fetch_note_history`) allow.
        if not may_read_note(
            user_id=ctx.user_id,
            note_type_code=NOTE_TYPE_TASK,
            note_id=note.note_id,
            owner_id=note.owner_id,
        ):
            continue
        out.append({"anchor_type": "note", "anchor_id": f"task:{note.note_id}"})
    return out


def task_anchors(task: TaskMaster, ctx: ToolContext) -> list[dict[str, str]]:
    """The anchors of a task the caller can already open, those the caller may open too."""
    anchors = []
    thread = _thread_anchor(task, ctx)
    if thread:
        anchors.append(thread)
    chat = _chat_anchor(task, ctx)
    if chat:
        anchors.append(chat)
    anchors.extend(_note_anchors(task, ctx))
    return anchors


#: The sentence the flag adds to the tool's description (declaration), after the existing text.
DESCRIPTION_ADDITION = (
    " `anchors` lists where this task's conversation and note are, when you may open them: "
    "`{anchor_type: \"thread\"}` is the chat thread the task was made from (its `anchor_id` is "
    "the thread root's message id, the `thread_id` of `fetch_chat_thread`), `chat` is the "
    "chat it was made in (the `chat_id`), and `note` is the task's note (`anchor_id` is "
    "`task:<note_id>`: call `fetch_note` with `note_type` \"task\" and that `note_id`). An "
    "empty list means there is none you can open, not that there is none."
)

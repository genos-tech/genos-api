"""Which object a conversation is about, and whether the person may still open it (MIP-1590).

An `AgentSession` can be bound to ONE thread, note, task or milestone (`chat_type`/`chat_id`/`thread_id`,
`note_type`/`note_id`, `task_id`). Three rules hang on that binding, and they live here so the ask, the
history list and the session endpoint apply the same ones:

  * `same_object`: a session named by `session_id` is continued under a thread or note context only if
    it is bound to THAT thread or note. A general (Cmd-K) session, another thread's or a note's is set
    aside, so asking about one object never writes into a conversation about something else (the task
    scope has had this rule since MIP-1057).
  * `object_readable`: the earlier answers of a conversation are about the object, so they are handed
    back (history list, `/agent/sessions/<id>/`) or given to the model again (an ask continued with the
    session id alone) only while the person can still open that object. The check is the access rule of
    the ask itself (`task_context.can_read`, `thread_summary.chat_readable`, `note_summary.note_readable`),
    in the team the session belongs to, and a plain session (no object) is always readable. A thread is
    readable by whoever is in its chat (a chat left or deleted hides its threads' conversations; a thread
    root deleted inside a chat the person is still in does not).
  * `scope_of`: which object, as ids only, for the history list. Never a title, a name or any text of the
    object: a row of the list may be shown to a person who has since lost access to it.

Read access only. Deleting a conversation and pinning one stay open to its owner whatever has happened to
the object: a person must be able to erase what they can no longer see.
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.search_engine.agent import note_summary, task_context, thread_summary


def scope_of(session) -> dict[str, Any] | None:
    """`{"kind": "task", "task_id"}`, `{"kind": "thread", "chat_type", "chat_id", "thread_id"}`,
    `{"kind": "note", "note_type", "note_id"}`, or None for a session about no object. Ids only.

    A task or milestone is its backing task row's id (`AgentSession.task_id`), whichever it is."""
    if session.task_id is not None:
        return {"kind": "task", "task_id": int(session.task_id)}
    if session.chat_type is not None and session.chat_id and session.thread_id:
        return {
            "kind": "thread",
            "chat_type": int(session.chat_type),
            "chat_id": str(session.chat_id),
            "thread_id": str(session.thread_id),
        }
    if session.note_type is not None and session.note_id is not None:
        return {
            "kind": "note",
            "note_type": int(session.note_type),
            "note_id": int(session.note_id),
        }
    return None


def _same_uuid(a: Any, b: Any) -> bool:
    """Two UUIDs as the same value however they are spelled (case, braces, a `UUID` or a string)."""
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except (ValueError, TypeError, AttributeError):
        return False


def same_object(session, *, thread_context: dict | None, note_context: dict | None) -> bool:
    """May `session` be continued under this thread or note context?

    With a thread context: only a session bound to exactly that thread. With a note context: only one
    bound to exactly that note. With neither: always (the caller decides what a bare `session_id` may
    do, as for a history pick). A session that is bound to a task never matches either.
    """
    if thread_context:
        return (
            session.task_id is None
            and session.note_type is None
            and session.chat_type == thread_context["chat_type"]
            and _same_uuid(session.chat_id, thread_context["chat_id"])
            and _same_uuid(session.thread_id, thread_context["thread_id"])
        )
    if note_context:
        return (
            session.task_id is None
            and session.chat_type is None
            and session.note_type == note_context["note_type"]
            and session.note_id == note_context["note_id"]
        )
    return True


def object_readable(session, user_id: str, memo: dict | None = None) -> bool:
    """Whether `user_id` may still open the object `session` is about; True for a plain session.

    `memo` is for a list of sessions: pass the same dict for every row of one request and an object
    shared by several sessions is checked once. Cheap checks only (no message or note body is loaded).
    """
    scope = scope_of(session)
    if scope is None:
        return True
    team_id = str(session.team_id)
    user_id = str(user_id)
    # What the answer depends on: a thread is readable by whoever is in its chat, so every thread of one
    # chat shares an answer.
    if scope["kind"] == "thread":
        key = ("thread", scope["chat_type"], scope["chat_id"])
    else:
        key = (scope["kind"], team_id, *(v for k, v in scope.items() if k != "kind"))
    if memo is not None and key in memo:
        return memo[key]
    readable = _readable(scope, team_id, user_id)
    if memo is not None:
        memo[key] = readable
    return readable


def _readable(scope: dict[str, Any], team_id: str, user_id: str) -> bool:
    if scope["kind"] == "task":
        return task_context.can_read(scope["task_id"], team_id, user_id)
    if scope["kind"] == "thread":
        return thread_summary.chat_readable(scope["chat_type"], scope["chat_id"], user_id)
    return note_summary.note_readable(
        note_type=scope["note_type"], note_id=scope["note_id"], user_id=user_id, team_id=team_id
    )

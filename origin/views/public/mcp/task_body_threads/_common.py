"""What the task-body thread tools share (MIP-1863): the thread id's shape, who may resolve, and what
each way collab can say no reads like to the caller. The note-thread equivalents live in
`note_threads/_common.py`; the sentences here name the task and `get_task`, not the note.

Nothing here echoes a body's text: it is the caller's own, but a refusal is logged and shown in
approval cards, and the task's text has no business in either.
"""

from __future__ import annotations

import re
from typing import Any

from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.services.archive import ARCHIVED_MESSAGE, archived_task_response
from origin.services.collab_push import ThreadWriteError
from origin.views.public.mcp.note_threads._common import actor_id

# What `get_task` returns in `body_comment_threads` as `thread_id`: an id BlockNote made, checked to be
# that shape before it is sent on (the same pattern collab holds it to).
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BLOCK_ID_HINT = (
    "Pass `block_id` to say which block (from `blocks` in `get_task`, or the block the anchor is in) "
    "and `occurrence` (1 = the first) if it repeats inside one block."
)
_READ_AGAIN = "Read the task with `get_task` to see its current comments on the body."
_OPEN_FIRST = (
    "Nobody has opened this task in the editor yet, so its body has no live document to hold a "
    "comment. Open the task once in Genos (or ask whoever created it to), then try again. "
    "Nothing was changed."
)


def refuse_if_archived(task: Any) -> None:
    """A comment on the body is a write: refuse it when the task's project is archived, before
    anything is sent to collab (MIP-1693, Ken 2026-11-22)."""
    if archived_task_response(task.task_id) is not None:
        raise ToolError(f"{ARCHIVED_MESSAGE} Nothing was changed.")


def clean_thread_id(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not _THREAD_ID.match(text):
        raise ToolError(
            "`thread_id` is the id of a comment thread on the task's body: take it from "
            "`body_comment_threads` in `get_task`."
        )
    return text


def may_resolve_any(task: Any, ctx: ToolContext) -> bool:
    """Whether the caller may resolve ANY thread on this task body: its assignee or its reporter.

    The narrower rule of the note tools (where the note's owner may) and of MIP-1100, applied to a
    task: the people the task is for. Anyone else may resolve only a thread they started, which collab
    checks against the thread's first comment.
    """
    me = actor_id(ctx)
    return me in {str(v) for v in (task.assignee_id, task.reporter_id) if v}


def create_refusal(err: ThreadWriteError) -> ToolError:
    """Why a new comment thread on the task body was not started (nothing written, except `unknown`)."""
    kind, code, facts = err.kind, err.code, err.facts
    if kind == "not_found" and code == "anchor_not_found":
        blocked = facts.get("blockedBlocks")
        extra = (
            " The text does appear in a code, math or diagram block, which cannot hold a comment."
            if isinstance(blocked, int) and blocked > 0
            else ""
        )
        return ToolError(
            "That text was not found in the task body. `anchor_text` must appear exactly as written "
            "in the body's plain text (same case and spacing, without markdown marks like ** or #), "
            "inside one paragraph, heading, list item, quote or table cell. Read the task with "
            "`get_task` and copy a passage from `content_text`." + extra + " Nothing was changed."
        )
    if kind == "not_found" and code == "block_not_found":
        return ToolError(
            "That block was not found in this task body (it may have changed). Read the task with "
            "`get_task` to see its current body. Nothing was changed."
        )
    if kind == "not_found" and code == "no_document":
        return ToolError(_OPEN_FIRST)
    if kind == "conflict" and code == "anchor_ambiguous":
        matches = facts.get("matches")
        return ToolError(
            f"That text appears {matches if isinstance(matches, int) else 'more than once'} times in "
            f"the task body. {BLOCK_ID_HINT} Nothing was changed."
        )
    if kind == "refused" and code == "occurrence_out_of_range":
        matches = facts.get("matches")
        return ToolError(
            f"`occurrence` is past the last match: the text appears "
            f"{matches if isinstance(matches, int) else 'fewer times'} time(s). Nothing was changed."
        )
    if kind == "unsupported":
        return ToolError(
            "Starting a comment on a task body is not available on this server yet: the comment "
            "service has not been updated. Nothing was changed."
        )
    if kind == "unknown":
        return ToolError(
            "The comment was sent but no answer came back, so it may or may not have been added. "
            + _READ_AGAIN
            + " Otherwise it may appear twice."
        )
    if kind == "refused":
        return ToolError(f"The comment service rejected the request: {err}.")
    return ToolError(
        "This task's comments could not be reached right now. Nothing was changed; try again in a moment."
    )


def refusal(err: ThreadWriteError, *, thread_id: str, resolving: bool = False) -> ToolError:
    """A collab "no" on a reply or a resolve, as the sentence the caller reads."""
    kind = err.kind
    if kind == "not_found" and err.code == "no_document":
        return ToolError(_OPEN_FIRST)
    if kind == "not_found":
        return ToolError(
            f"Thread {thread_id} was not found on this task body (it may have been deleted). "
            + _READ_AGAIN
        )
    if kind == "forbidden":
        return ToolForbidden(
            "Only the thread's author or the task's assignee or reporter can resolve it. "
            "You can add a reply with `reply_to_task_body_comment` instead."
        )
    if kind == "unsupported":
        return ToolError(
            "Replying to and resolving comments on a task body is not available on this server yet: "
            "the comment service has not been updated. Nothing was changed."
        )
    if kind == "unknown":
        if resolving:
            return ToolError(
                "The resolve was sent but no answer came back; try again, it changes nothing twice."
            )
        return ToolError(
            "The reply was sent but no answer came back, so it may or may not have been added. "
            + _READ_AGAIN
            + " Otherwise it may appear twice."
        )
    if kind == "refused":
        return ToolError(f"The comment service rejected the request: {err}.")
    return ToolError(
        "This task's comments could not be reached right now. Nothing was changed; try again in a moment."
    )

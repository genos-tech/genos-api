"""`comment_on_task_body` (MIP-1863): MCP-only, see `task_body_threads/__init__.py`.

Starts a NEW comment thread on a passage of a task's body, as the caller: the first comment, anchored to
the passage the way the editor anchors one (a `comment` mark on those words). Collab finds the passage in
the live document, in one transaction with the write, and refuses, writing nothing, when it is not there
or is ambiguous. A task body nobody has opened has no document: that is refused with what to do (a task
body is not seeded from the stored text here, unlike a note, MIP-1670).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.task_access import load_task
from origin.services import collab_push
from origin.views.public.mcp.note_threads._common import actor_id, reduced_via
from origin.views.public.mcp.resolve import BLOCK_ID
from origin.views.public.mcp.task_body_threads._common import create_refusal, refuse_if_archived

MAX_TEXT_CHARS = 4000
MAX_LINES = 50
MAX_ANCHOR_CHARS = 500
MAX_OCCURRENCE = 1000


def _text(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ToolError("`text` is required: the comment, as plain text.")
    if len(text) > MAX_TEXT_CHARS:
        raise ToolError(
            f"`text` is {len(text)} characters; a comment can be at most {MAX_TEXT_CHARS}."
        )
    if len([line for line in text.splitlines() if line.strip()]) > MAX_LINES:
        raise ToolError(f"`text` has more than {MAX_LINES} lines; shorten the comment.")
    return text


def _anchor(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ToolError(
            "`anchor_text` is required: the passage the comment is about, copied exactly from the "
            "task body's plain text (`content_text` in `get_task`)."
        )
    if len(text) > MAX_ANCHOR_CHARS:
        raise ToolError(
            f"`anchor_text` is {len(text)} characters; a passage can be at most {MAX_ANCHOR_CHARS}. "
            "Quote the few words the comment is about."
        )
    return text


def _block_id(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    text = raw.strip() if isinstance(raw, str) else ""
    if not BLOCK_ID.match(text):
        raise ToolError("`block_id` is the id of one block: take it from `blocks` in `get_task`.")
    return text


def _occurrence(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    bad = ToolError(f"`occurrence` must be a whole number from 1 to {MAX_OCCURRENCE}.")
    if isinstance(raw, bool):
        raise bad
    try:
        number = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise bad from None
    if isinstance(raw, float) and not raw.is_integer():
        raise bad
    if not 1 <= number <= MAX_OCCURRENCE:
        raise bad
    return number


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = _text(args.get("text"))
    anchor = _anchor(args.get("anchor_text"))
    block_id = _block_id(args.get("block_id"))
    occurrence = _occurrence(args.get("occurrence"))
    task = load_task(args.get("task_id"), ctx, action="comment on")
    refuse_if_archived(task)
    try:
        done = collab_push.create_thread_in(
            collab_push.task_body_document(task.task_id),
            user_id=actor_id(ctx),
            text=text,
            anchor_text=anchor,
            block_id=block_id,
            occurrence=occurrence,
            via=reduced_via(ctx),
        )
    except collab_push.ThreadWriteError as err:
        raise create_refusal(err)
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "thread_id": done.get("threadId"),
        "comment_id": done.get("commentId"),
        "block_id": done.get("blockId"),
        "occurrence": done.get("occurrence"),
        "matches": done.get("matches"),
        "__summary__": f"Started a comment thread on the body of {task.display_id}, on text in block {done.get('blockId')}.",
    }


COMMENT_ON_TASK_BODY = Tool(
    name="comment_on_task_body",
    description=(
        "Start a new comment thread on a passage of a task's body, as the caller. REQUIRES USER "
        "APPROVAL. Pass `task_id`, `anchor_text` (the passage, copied exactly from the body's plain "
        "text, `content_text` in `get_task`) and `text` (the comment, plain text)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task's id."},
            "anchor_text": {
                "type": "STRING",
                "description": f"The passage, copied exactly from the body's text (at most {MAX_ANCHOR_CHARS} characters).",
            },
            "text": {
                "type": "STRING",
                "description": f"The comment, plain text (at most {MAX_TEXT_CHARS} characters).",
            },
            "block_id": {
                "type": "STRING",
                "description": "Optional: only look for the passage in this block (`block_id` from `blocks`).",
            },
            "occurrence": {
                "type": "INTEGER",
                "description": "Optional: which occurrence when the passage appears more than once (1 = the first).",
            },
        },
        "required": ["task_id", "anchor_text", "text"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)

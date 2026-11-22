"""`comment_on_note` (MIP-1644): MCP-only, see `note_threads/__init__.py`.

Starts a NEW comment thread on a passage of a note, as the caller: the first comment, anchored to the
passage the way the editor anchors one (a `comment` mark on those words). Whoever may open the note may
comment on it in the editor (viewers included), so the access check is the one `get_note` makes, as for
`reply_to_note_comment`.

The passage is named by its text, which may occur 0, 1 or several times, so it is matched EXACTLY in the
note's plain text and refused, with nothing written, when it is not found or is ambiguous. The matching
happens where the live document is (genos-collab, `create-thread`): the api only checks the arguments,
the caller's access, and turns each way collab can say no into a sentence a model can act on.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import collab_push
from origin.services.note_comment_notify import notify_note_comment
from origin.views.public.mcp.note_blocks import _stored
from origin.views.public.mcp.note_threads._common import (
    actor_id,
    create_refusal,
    reduced_via,
    refuse_if_archived,
)
from origin.views.public.mcp.resolve import BLOCK_ID

# One comment, not a document: the same size a reply is.
MAX_TEXT_CHARS = 4000
MAX_LINES = 50
# A passage, not a page: the sentence or phrase the comment is about.
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
            "`anchor_text` is required: the passage the comment is about, copied exactly from "
            "the note's text."
        )
    if len(text) > MAX_ANCHOR_CHARS:
        raise ToolError(
            f"`anchor_text` is {len(text)} characters; a passage can be at most "
            f"{MAX_ANCHOR_CHARS}. Quote the few words the comment is about."
        )
    return text


def _block_id(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    text = raw.strip() if isinstance(raw, str) else ""
    if not BLOCK_ID.match(text):
        raise ToolError(
            "`block_id` is the id of one block: take it from `blocks` in `get_note` "
            "(call it with `include_blocks: true`), or pass the block's link as `note_id`."
        )
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
    ref = load_readable_note(args, ctx)
    refuse_if_archived(ref)

    def ask(seed_blocks=None):
        return collab_push.create_note_thread(
            ref.family,
            ref.note_id,
            user_id=actor_id(ctx),
            text=text,
            anchor_text=anchor,
            block_id=block_id,
            occurrence=occurrence,
            via=reduced_via(ctx),
            seed_blocks=seed_blocks,
        )

    try:
        try:
            done = ask()
        except collab_push.ThreadWriteError as err:
            # A note nobody has opened has no document. The refusal wrote nothing, so asking again is
            # safe, ONCE and with the note's stored body for collab to make the document from
            # (MIP-1670). Nothing else is retried: a second POST of anything else is a second thread.
            stored = getattr(ref.note, "body", None)
            if err.code != "no_document" or not isinstance(stored, list) or not stored:
                raise
            done = ask(seed_blocks=_stored.with_effective_ids(stored))
    except collab_push.ThreadWriteError as err:
        raise create_refusal(err)
    # The comment is in the note: tell the people it reaches, as the editor's own comment would
    # (best-effort, never fails this call; the key's owner gets their own switchable notice).
    notify_note_comment(
        ref.note,
        note_type=ref.code,
        actor_id=actor_id(ctx),
        comment_id=str(done.get("commentId") or ""),
        text=text,
    )
    return {
        "note_id": ref.note_id,
        "note_type": ref.family,
        # What the owner's notice names (`mcp_notice`): the note's title and, for a task note, its task.
        "title": getattr(ref.note, "title", None),
        "task_id": getattr(ref.note, "task_id", None),
        "thread_id": done.get("threadId"),
        "comment_id": done.get("commentId"),
        "block_id": done.get("blockId"),
        "occurrence": done.get("occurrence"),
        "matches": done.get("matches"),
        # True when the note had never been opened and its document was made from its stored body for
        # this comment (MIP-1670).
        "document_created": done.get("seeded") is True,
        "__summary__": (
            f"Started a comment thread on note {ref.note_id}, on text in block "
            f"{done.get('blockId')}."
        ),
    }


COMMENT_ON_NOTE = Tool(
    name="comment_on_note",
    description=(
        "Start a new comment thread on a passage of a note, as the caller. REQUIRES USER "
        "APPROVAL. Pass `note_type` with `note_id`, `anchor_text` (the passage, copied exactly "
        "from the note) and `text` (the comment, plain text)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "Which kind of note (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "The note's id."},
            "anchor_text": {
                "type": "STRING",
                "description": (
                    "The passage the comment is about, copied exactly from the note's text "
                    f"(at most {MAX_ANCHOR_CHARS} characters)."
                ),
            },
            "text": {
                "type": "STRING",
                "description": (
                    f"The comment, plain text (at most {MAX_TEXT_CHARS} characters). Each line "
                    "becomes a paragraph; no formatting or @-mentions."
                ),
            },
            "block_id": {
                "type": "STRING",
                "description": "Optional: only look for the passage in this block (`block_id` from `blocks`).",
            },
            "occurrence": {
                "type": "INTEGER",
                "description": (
                    "Optional: which occurrence to comment on when the passage appears more than "
                    "once (1 = the first, in the note's order, or in `block_id`)."
                ),
            },
        },
        "required": ["note_type", "note_id", "anchor_text", "text"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)

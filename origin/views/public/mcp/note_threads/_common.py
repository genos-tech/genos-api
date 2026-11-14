"""What the two note-thread tools share (MIP-1100): who the caller is, whether they own the note,
how a reply is marked, and what each way collab can say no reads like to the caller.

Reading the arguments stays in each tool's own module (the catalog contract test reads a tool's
module for the keys it takes).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from origin.middleware.current_user import get_current_source
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.note_access import NoteRef
from origin.services.collab_push import ThreadWriteError
from origin.views.public.mcp.via_privacy import via_for_viewer
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    ROLE_OWNER,
    get_effective_role,
)

# What `get_note` returns as `thread_id`: an id BlockNote made (a uuid), checked to be that shape
# before it is sent on, the same pattern collab holds it to.
_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The kinds of credential a reply can arrive through; the family is a short lowercase name.
_VIA_KINDS = frozenset({"api_key", "oauth", "app", "agent"})
_VIA_APP = re.compile(r"^[a-z0-9_-]{1,32}$")

_READ_AGAIN = "Read the note with `get_note` to see its current threads."


def clean_thread_id(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not _THREAD_ID.match(text):
        raise ToolError(
            "`thread_id` is the id of a comment thread: take it from `comment_threads` in "
            "`get_note`."
        )
    return text


def actor_id(ctx: ToolContext) -> str:
    """The caller as the editor writes a user: their UUID, lowercase, with hyphens."""
    try:
        return str(uuid.UUID(str(ctx.user_id)))
    except ValueError:
        raise ToolError("This call has no signed-in user.")


def is_note_owner(ref: NoteRef, ctx: ToolContext) -> bool:
    """The note's owner: a personal note's own author, or whoever holds the Owner role on it
    (`get_effective_role`: an explicit grant, or a Team Notes folder's owner). The same notion
    `delete_note` and `manage_note_sharing` use; nothing here is a new rule."""
    if ref.code == NOTE_TYPE_PERSONAL and str(getattr(ref.note, "owner_id", "") or "") == str(
        ctx.user_id
    ):
        return True
    return get_effective_role(ctx.user_id, ref.code, ref.note_id) == ROLE_OWNER


def reduced_via(ctx: ToolContext) -> dict[str, Any] | None:
    """How this write arrived, as ANY reader of the note may read it, for the reply's marker.

    The whole Yjs doc goes to everyone who opens the note, so the marker is the reduced `via`
    (`via_for_viewer` for nobody in particular): the kind of credential and a connected app's
    official family, never a key's name, prefix or a connection id (MIP-1485).
    """
    shown = via_for_viewer(get_current_source(), ctx.user_id, None)
    if not isinstance(shown, dict) or shown.get("kind") not in _VIA_KINDS:
        return None
    out: dict[str, Any] = {"kind": shown["kind"]}
    app = shown.get("app")
    if isinstance(app, str) and _VIA_APP.match(app):
        out["app"] = app
    return out


def refusal(err: ThreadWriteError, *, thread_id: str, resolving: bool = False) -> ToolError:
    """A collab "no" as the sentence the caller reads. Nothing here echoes a note's text."""
    kind = err.kind
    if kind == "not_found":
        return ToolError(
            f"Thread {thread_id} was not found on this note (it may have been deleted). "
            + _READ_AGAIN
        )
    if kind == "forbidden":
        return ToolForbidden(
            "Only the thread's author or the note's owner can resolve it. "
            "You can add a reply with `reply_to_note_comment` instead."
        )
    if kind == "unsupported":
        return ToolError(
            "Replying to and resolving note comments is not available on this server yet: the "
            "comment service has not been updated. Nothing was changed."
        )
    if kind == "unknown":
        return ToolError(
            (
                "The resolve was sent but no answer came back; try again, it changes nothing twice. "
                if resolving
                else "The reply was sent but no answer came back, so it may or may not have been "
                "added. Read the note with `get_note` before replying again, or it may appear twice."
            )
        )
    if kind == "refused":
        return ToolError(f"The comment service rejected the request: {err}.")
    return ToolError(
        "This note's comments could not be reached right now. Nothing was changed; "
        "try again in a moment."
    )

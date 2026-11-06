"""A note's comment threads, shaped for an agent (MIP-1064).

BlockNote comments are stored in the note's Yjs doc (genos-collab), so `get_note`,
which reads the `body` column, never showed them: a reviewer's feedback on a note
was invisible to Claude over MCP. `read_note_threads` fetches them from collab;
this turns them into what a model can use: who said what, when, on which text,
and whether the thread is settled.

MCP only, on purpose. `fetch_note` is also the in-app agent's tool, and adding
output to it changes what that model sees, which waits for the December eval
(MIP-1064). The MCP layer calls this after `fetch_note` has passed its access
check, so a thread is shown to exactly the people who can read the note body.
"""

from __future__ import annotations

import logging
from typing import Any

from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext, wrap_workspace_content
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.services.collab_push import read_note_threads

logger = logging.getLogger(__name__)

# A note with more threads than this is a document under review by a crowd, and
# a model reading it needs the newest open ones, not a hundred. Open threads come
# first, so the cap drops settled ones before live ones.
MAX_THREADS = 50
# One comment, not a document pasted into a thread.
MAX_COMMENT_CHARS = 4000


def _clip(text: str) -> str:
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + "…"


def _person(user_id: Any, names: dict[str, str]) -> dict[str, Any] | None:
    if not isinstance(user_id, str) or not user_id:
        return None
    name = names.get(user_id)
    return {"user_id": user_id, "username": wrap_workspace_content(name) if name else None}


def _comment(raw: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    deleted = bool(raw.get("deleted"))
    created, updated = raw.get("createdAt"), raw.get("updatedAt")
    out: dict[str, Any] = {
        "author": _person(raw.get("userId"), names),
        "ts": created,
        "deleted": deleted,
    }
    if deleted:
        return out
    if created and updated and updated != created:
        out["edited"] = True
    out["markdown"] = wrap_workspace_content(_clip(blocks_to_markdown(raw.get("body"))))
    return out


def _thread(raw: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "thread_id": raw.get("id"),
        "resolved": bool(raw.get("resolved")),
    }
    if out["resolved"]:
        out["resolved_by"] = _person(raw.get("resolvedBy"), names)
        out["resolved_at"] = raw.get("resolvedAt")
    anchor = raw.get("anchorText")
    out["anchor_text"] = wrap_workspace_content(anchor) if anchor else None
    out["comments"] = [_comment(c, names) for c in raw.get("comments") or [] if isinstance(c, dict)]
    return out


def note_comment_fields(
    note_type: str, note_id: int, ctx: ToolContext, *, include_resolved: bool = False
) -> dict[str, Any]:
    """The keys `get_note` adds on MCP: `comment_threads` and, when there is
    something to say, `comment_threads_note`.

    `comment_threads` is a list (open threads first, oldest first), or `null`
    when the comments could not be read: the body is still complete then, and the
    note says so. Resolved threads are left out unless asked for, with a count of
    how many were left so the caller knows they exist.
    """
    raw_threads = read_note_threads(note_type, note_id)
    if raw_threads is None:
        return {
            "comment_threads": None,
            "comment_threads_note": (
                "This note's comments could not be read right now. The body above is "
                "complete; try again in a moment if the comments matter."
            ),
        }

    threads = [t for t in raw_threads if isinstance(t, dict) and t.get("id")]
    open_threads = [t for t in threads if not t.get("resolved")]
    resolved = [t for t in threads if t.get("resolved")]
    shown = open_threads + (resolved if include_resolved else [])
    truncated = len(shown) > MAX_THREADS
    shown = shown[:MAX_THREADS]

    user_ids = {
        uid
        for t in shown
        for uid in [
            t.get("resolvedBy"),
            *[c.get("userId") for c in t.get("comments") or [] if isinstance(c, dict)],
        ]
        if isinstance(uid, str)
    }
    names = LabelAccess(ctx).usernames(user_ids) if user_ids else {}

    fields: dict[str, Any] = {"comment_threads": [_thread(t, names) for t in shown]}
    notes = []
    if resolved and not include_resolved:
        s = "s" if len(resolved) != 1 else ""
        notes.append(
            f"{len(resolved)} resolved thread{s} not shown; pass `include_resolved: true` to read {'them' if s else 'it'}."
        )
    if truncated:
        notes.append(f"Only the first {MAX_THREADS} threads are shown.")
    if notes:
        fields["comment_threads_note"] = " ".join(notes)
    return fields

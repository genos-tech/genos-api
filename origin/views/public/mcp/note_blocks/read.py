"""The `blocks` that `get_note(include_blocks=true)` adds on MCP (MIP-1646).

From the note's LIVE doc through genos-collab, so what a person has just typed is in what is shown. A
note nobody has opened has no doc: its stored `body` is then the truth and is read here. When neither
can be read the answer is `blocks: null` with a note, never an empty list: "no blocks" and "could not
read" must not look alike (the same rule as `comment_threads`).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import collab_push
from origin.views.public.mcp.note_blocks import _stored

MAX_BLOCKS = 500


def _from_collab(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_id": raw.get("id"),
        "type": raw.get("type"),
        "depth": raw.get("depth", 0),
        "parent_block_id": raw.get("parentId"),
        "text": raw.get("text", ""),
        "block_hash": raw.get("hash"),
        "has_children": bool(raw.get("hasChildren")),
    }


def note_block_fields(note_type: str, note_id: int, ctx: ToolContext) -> dict[str, Any]:
    live = collab_push.read_note_blocks(
        "personal" if note_type not in ("task", "chat") else note_type, note_id
    )
    if live is None:
        return {
            "blocks": None,
            "blocks_note": (
                "This note's blocks could not be read right now. The body above is complete; "
                "try again in a moment before editing a block."
            ),
        }
    if live["exists"]:
        blocks = [_from_collab(b) for b in live["blocks"] if isinstance(b, dict)]
    else:
        ref = load_readable_note({"note_type": note_type, "note_id": note_id}, ctx)
        blocks = _stored.summarize(ref.note.body)
    out: dict[str, Any] = {"blocks": blocks[:MAX_BLOCKS]}
    if len(blocks) > MAX_BLOCKS:
        out["blocks_truncated"] = {"shown": MAX_BLOCKS, "total": len(blocks)}
        out["blocks_note"] = (
            f"Only the first {MAX_BLOCKS} of {len(blocks)} blocks are listed; a block past that "
            "cannot be named by this call."
        )
    return out

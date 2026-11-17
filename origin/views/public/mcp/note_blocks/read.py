"""The `blocks` that `get_note(include_blocks=true)` adds on MCP (MIP-1646).

From the note's LIVE doc through genos-collab, so what a person has just typed is in what is shown. A
note nobody has opened has no doc: its stored `body` is then the truth and is read here. When neither
can be read the answer is `blocks: null` with a note, never an empty list: "no blocks" and "could not
read" must not look alike (the same rule as `comment_threads`).
"""

from __future__ import annotations

import json
from typing import Any

from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import collab_push
from origin.views.public.mcp import result_limits
from origin.views.public.mcp.note_blocks import _stored

#: The most blocks one answer lists.
MAX_BLOCKS = 500
#: With the result cap on (`MCP_RESULT_LIMITS`), the size of one page of blocks, in characters of the
#: rendered answer. The body part of `get_note` takes what is left of the cap, so a page that is
#: smaller than the cap leaves the body room instead of being squeezed by it (MIP-1666: a 29,000
#: character note listed 70 of its 238 blocks, with no way to ask for the rest).
CAPPED_PAGE_CHARS = 9_000
#: The longest phrase `blocks_matching` takes.
MAX_MATCHING_CHARS = 200


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


def _rendered_size(block: dict[str, Any]) -> int:
    """About what one block costs in the answer's text, which is indented JSON (`render` in tools.py),
    and inside a list: four more spaces on each line."""
    text = json.dumps(block, indent=2, ensure_ascii=False, default=str)
    return len(text) + 4 * (text.count("\n") + 1) + 1


def _page(blocks: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    """The blocks from `offset`: at most `MAX_BLOCKS`, and with the result cap on, as many as fit
    `CAPPED_PAGE_CHARS` (always at least one, so a page can never be empty while blocks remain)."""
    rest = blocks[offset : offset + MAX_BLOCKS]
    if not result_limits.limits_on():
        return rest
    page: list[dict[str, Any]] = []
    used = 0
    for block in rest:
        size = _rendered_size(block)
        if page and used + size > CAPPED_PAGE_CHARS:
            break
        page.append(block)
        used += size
    return page


def blocks_offset_arg(raw: Any) -> int:
    """`blocks_offset`: a whole number from 0, else a sentence naming it."""
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        raise ToolError("`blocks_offset` must be a whole number from 0.")
    try:
        number = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ToolError("`blocks_offset` must be a whole number from 0.") from None
    if isinstance(raw, float) and not raw.is_integer():
        raise ToolError("`blocks_offset` must be a whole number from 0.")
    if number < 0:
        raise ToolError("`blocks_offset` must be a whole number from 0.")
    return number


def blocks_matching_arg(raw: Any) -> str | None:
    """`blocks_matching`: the phrase, trimmed, or None when none was given."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ToolError("`blocks_matching` must be text: a word or phrase a block contains.")
    text = raw.strip()
    if not text:
        return None
    if len(text) > MAX_MATCHING_CHARS:
        raise ToolError(
            f"`blocks_matching` is too long ({len(text)} characters; the limit is "
            f"{MAX_MATCHING_CHARS}). Use a few distinctive words from the block."
        )
    return text


def note_block_fields(
    note_type: str,
    note_id: int,
    ctx: ToolContext,
    *,
    matching: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
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
    in_note = len(blocks)
    if matching is not None:
        needle = matching.casefold()
        blocks = [b for b in blocks if needle in str(b.get("text") or "").casefold()]
    page = _page(blocks, offset)
    out: dict[str, Any] = {"blocks": page}
    end = offset + len(page)
    more = end < len(blocks)
    if more:
        out["blocks_next_offset"] = end
    if more or offset:
        out["blocks_truncated"] = {"shown": len(page), "total": len(blocks)}
        if offset:
            out["blocks_truncated"]["offset"] = offset
    notes: list[str] = []
    if matching is not None:
        if blocks:
            notes.append(
                f"{len(blocks)} of {in_note} blocks contain that text (each block's first "
                f"{_stored.MAX_TEXT_CHARS} characters are searched, ignoring case)."
            )
        else:
            notes.append(
                f"No block of this note's {in_note} contains that text (each block's first "
                f"{_stored.MAX_TEXT_CHARS} characters are searched, ignoring case). Try fewer, "
                "more distinctive words."
            )
    if offset and not page and blocks:
        notes.append(f"`blocks_offset` {offset} is past the last block ({len(blocks)}).")
    if more:
        notes.append(
            f"Listing blocks {offset + 1}-{end} of {len(blocks)}. Pass `blocks_offset: {end}` for "
            "the next page, or find a block by its text with `blocks_matching`."
        )
    if notes:
        out["blocks_note"] = " ".join(notes)
    return out

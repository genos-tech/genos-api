"""Partial edits of a task body, and the hash that guards them (MIP-824).

`update_task.content_text` replaces the whole description, so changing one
sentence meant resending the full body, and an edit made between the read and
the write was silently overwritten. Three things here fix that:

* `body_hash` - a short digest of the body AS A READER SEES IT. `fetch_task`
  returns it; `update_task(base_body_hash=...)` refuses to write when the
  stored body no longer hashes to it.
* `append_blocks` / `prepend_blocks` - splice a markdown fragment into the
  stored blocks, leaving the existing blocks (and their ids) alone.
* `replace_in_body` - find-and-replace on the body's markdown, exactly one
  match required. Only the top-level blocks the match touches are rebuilt (MIP-1645); every other
  block keeps its stored JSON byte for byte, ids and marks included.

The hash is taken over `without_untouched_prompts(body)`, the same view
`fetch_task` shows (MIP-825). Hashing the raw stored body instead would make
every task that still carries a template prompt hash differently from what the
caller read. Block ids are part of the digest: it answers "is this the body I
read", and the editor keeps ids stable across saves.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.blocknote_md import EntityChipResolver, task_body_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.template_prompts import _is_blank, without_untouched_prompts


def shown_body(body: Any) -> Any:
    """The body as `fetch_task` shows it: untouched template prompts removed."""
    return without_untouched_prompts(body) or []


def body_hash(body: Any) -> str:
    """16 hex chars identifying the body a reader was shown."""
    canonical = json.dumps(shown_body(body), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _fragment_blocks(
    markdown: str, argument: str, resolver: EntityChipResolver | None
) -> list[dict[str, Any]]:
    """The blocks of a fragment WITHOUT the trailing blank paragraph
    `task_body_blocks` adds (a spliced fragment must not leave a blank line
    in the middle of the body)."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ToolError(f"`{argument}` must be non-empty markdown.")
    blocks = task_body_blocks(markdown, entity_chip_resolver=resolver)
    return blocks[:-1] if blocks and _is_blank(blocks[-1]) else blocks


def append_blocks(
    stored: Any, markdown: str, resolver: EntityChipResolver | None
) -> list[dict[str, Any]]:
    """`stored` with the fragment added after the last real block, ahead of the
    editor's trailing blank paragraph."""
    fragment = _fragment_blocks(markdown, "body_append", resolver)
    current = list(stored) if isinstance(stored, list) else []
    if current and _is_blank(current[-1]):
        return [*current[:-1], *fragment, current[-1]]
    if not current:
        return task_body_blocks(markdown, entity_chip_resolver=resolver)
    return [*current, *fragment]


def prepend_blocks(
    stored: Any, markdown: str, resolver: EntityChipResolver | None
) -> list[dict[str, Any]]:
    """`stored` with the fragment added before its first block."""
    fragment = _fragment_blocks(markdown, "body_prepend", resolver)
    current = list(stored) if isinstance(stored, list) else []
    if not current:
        return task_body_blocks(markdown, entity_chip_resolver=resolver)
    return [*fragment, *current]


def _window_markdown(blocks: list[Any], first: int, last: int) -> str:
    return blocks_to_markdown(blocks[first : last + 1])


def _smallest_window(blocks: list[Any], old_text: str) -> tuple[int, int] | None:
    """`(first, last)`: the smallest run of top-level blocks whose own markdown contains `old_text`,
    or None when no run does.

    "Contains" only ever grows as a run is widened, so both ends are found by bisection (a handful of
    renders, not one per block). The body has exactly one match (the caller checked), so the smallest
    run is the one the match sits in. A run is rendered ON ITS OWN, so a match that only exists in the
    whole body's rendering (a numbered list item's number, which restarts inside a run) is not found
    here and the caller rebuilds the whole body instead.
    """
    n = len(blocks)
    if n == 0 or old_text not in _window_markdown(blocks, 0, n - 1):
        return None
    lo, hi = 0, n - 1  # smallest `last` such that blocks[0..last] contains it
    while lo < hi:
        mid = (lo + hi) // 2
        if old_text in _window_markdown(blocks, 0, mid):
            hi = mid
        else:
            lo = mid + 1
    last = lo
    lo, hi = 0, last  # largest `first` such that blocks[first..last] contains it
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if old_text in _window_markdown(blocks, mid, last):
            lo = mid
        else:
            hi = mid - 1
    return lo, last


def _unchanged_edges(old: list[Any], new: list[dict[str, Any]]) -> tuple[int, int]:
    """`(head, tail)`: how many blocks at the start and at the end of the window came out of the edit
    with the same markdown they went in with. They are kept as stored. A match that starts in the blank
    line between two blocks (to delete a whole paragraph) pulls its neighbour into the window although
    that neighbour's words did not change."""
    limit = min(len(old), len(new))
    head = 0
    while head < limit and blocks_to_markdown([old[head]]) == blocks_to_markdown([new[head]]):
        head += 1
    tail = 0
    while tail < limit - head and blocks_to_markdown(
        [old[len(old) - 1 - tail]]
    ) == blocks_to_markdown([new[len(new) - 1 - tail]]):
        tail += 1
    return head, tail


def _carry_ids(old: list[Any], new: list[dict[str, Any]]) -> None:
    """Give rebuilt blocks the ids of the blocks they replace, where it is plain which is which: the
    same number of blocks of the same types, or else the first block when its type did not change.
    A block link (MIP-146) and anything else naming a block by id keeps working through an edit of
    the words inside it."""
    if not old or not new:
        return
    if len(old) == len(new):
        for was, now in zip(old, new):
            if was.get("type") == now.get("type") and was.get("id"):
                now["id"] = was["id"]
    elif old[0].get("type") == new[0].get("type") and old[0].get("id"):
        new[0]["id"] = old[0]["id"]


def replace_in_body(
    stored: Any, old_text: Any, new_text: Any, resolver: EntityChipResolver | None
) -> list[dict[str, Any]] | None:
    """The body with `old_text` swapped for `new_text`, or None when that
    changes nothing.

    Matching runs on the markdown `fetch_task` returned as `content_markdown`
    (same renderer, same prompt filtering), so a string copied from that field
    is a string that is found here. Blocks are not matched directly: one
    sentence is often several styled runs and chips. Exactly one match is
    required; zero or several are refused with the count, never guessed at.

    Only the run of top-level blocks the match sits in is turned back into
    blocks (through `task_body_blocks`, the path `content_text` takes); every
    other block is returned exactly as stored, so its id, its props and its
    marks survive an edit made somewhere else (MIP-1645; before it, one changed
    word gave every block of the body a new id). A rebuilt block takes over the
    id of the block it replaces when that is plain (`_carry_ids`). A match that
    only exists in the whole body's rendering falls back to rebuilding it all, as
    before. A nested block is part of its top-level block: editing a child
    rebuilds that top-level block and what is under it.
    """
    if not isinstance(old_text, str) or not old_text:
        raise ToolError("`body_replace.old_text` must be a non-empty string.")
    if not isinstance(new_text, str):
        raise ToolError("`body_replace.new_text` must be a string (use '' to delete the match).")
    shown = shown_body(stored)
    markdown = blocks_to_markdown(shown)
    found = markdown.count(old_text)
    if found != 1:
        raise ToolError(
            f"`body_replace.old_text` matched {found} times in the task body; it must match "
            "exactly once. Copy a longer, unique stretch from `content_markdown`. "
            "Nothing was changed."
        )
    replaced = markdown.replace(old_text, new_text, 1)
    if replaced == markdown:
        return None

    window = _smallest_window(shown, old_text) if isinstance(shown, list) else None
    if window is None:
        return task_body_blocks(replaced, entity_chip_resolver=resolver)
    first, last = window
    old_run = shown[first : last + 1]
    new_markdown = _window_markdown(shown, first, last).replace(old_text, new_text, 1)
    rebuilt = (
        _fragment_blocks(new_markdown, "body_replace", resolver) if new_markdown.strip() else []
    )
    head, tail = _unchanged_edges(old_run, rebuilt)
    old_mid = old_run[head : len(old_run) - tail]
    new_mid = rebuilt[head : len(rebuilt) - tail]
    _carry_ids(old_mid, new_mid)
    result = [
        *shown[: first + head],
        *new_mid,
        *shown[last + 1 - tail :],
    ]
    # A body must not end up with no block at all (the editor keeps at least one line).
    return result or task_body_blocks("", entity_chip_resolver=resolver) or result

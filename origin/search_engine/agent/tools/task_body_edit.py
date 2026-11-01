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
  match required.

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


def replace_in_body(
    stored: Any, old_text: Any, new_text: Any, resolver: EntityChipResolver | None
) -> list[dict[str, Any]] | None:
    """The body with `old_text` swapped for `new_text`, or None when that
    changes nothing.

    Matching runs on the markdown `fetch_task` returned as `content_markdown`
    (same renderer, same prompt filtering), so a string copied from that field
    is a string that is found here. Blocks are not matched directly: one
    sentence is often several styled runs and chips. The result goes back
    through `task_body_blocks`, the path `content_text` takes, which assigns
    new block ids. Exactly one match is required; zero or several are refused
    with the count, never guessed at.
    """
    if not isinstance(old_text, str) or not old_text:
        raise ToolError("`body_replace.old_text` must be a non-empty string.")
    if not isinstance(new_text, str):
        raise ToolError("`body_replace.new_text` must be a string (use '' to delete the match).")
    markdown = blocks_to_markdown(shown_body(stored))
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
    return task_body_blocks(replaced, entity_chip_resolver=resolver)

"""The message a `scheduled_message` schedule stores and posts (MIP-2482).

The member writes it in the chat composer's editor; the schedule keeps the BlockNote blocks in
`params["body"]`, the same shape `Message.body` has, and the run posts them as that member. This
module is only about the message itself: is it one that can be posted, and what is its first line.
No query, no write.

Files are refused. An image or file in a chat body points at an upload made for ONE chat
(`channels/<id>/files`), and the schedule's chat can be changed later, so a stored body could end
up showing a file the chat's readers were never given.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from origin.search_engine.text_extraction import extract_text
from origin.views.chat.modules.common import generate_first_line

# As stored (UTF-8 JSON). A normal chat message is a few hundred bytes; this holds many pages.
MAX_BODY_BYTES = 40_000
MAX_BLOCKS = 500
MAX_DEPTH = 12
PREVIEW_CHARS = 120

FILE_TYPES = frozenset({"image", "file", "video", "audio"})
# Blocks whose content is inline text. Anything else with no `content` (a divider, a math or
# diagram block) is content by itself, as the composer counts it (`messageBlocks.ts`).
_TEXT_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "quote",
        "bulletListItem",
        "numberedListItem",
        "checkListItem",
        "toggleListItem",
        "codeBlock",
    }
)

EMPTY = "Write the message to send."
SHAPE = "The message could not be read. Write it again in the editor."
FILES = "A scheduled message cannot carry images or files. Remove them and save again."
TOO_LONG = "The message is too long. Shorten it, or split it into two schedules."


class MessageProblem(ValueError):
    """Why a body cannot be stored, as a sentence for the member."""


def _inline_has_content(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("type")
    if kind in (None, "text"):
        return isinstance(item.get("text"), str) and bool(item["text"].strip())
    if kind == "link":
        return any(_inline_has_content(i) for i in item.get("content") or [])
    return True  # a mention, a # reference chip, an emoji


def _block_has_content(block: dict[str, Any]) -> bool:
    content = block.get("content")
    if isinstance(content, list):
        if any(_inline_has_content(i) for i in content):
            return True
    elif content is not None:
        return True  # a table
    elif block.get("type") not in _TEXT_TYPES:
        return True
    return any(_block_has_content(c) for c in block.get("children") or [] if isinstance(c, dict))


def _check_blocks(blocks: list[Any]) -> None:
    count = 0
    stack: list[tuple[Any, int]] = [(b, 1) for b in blocks]
    while stack:
        block, depth = stack.pop()
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise MessageProblem(SHAPE)
        if block["type"] in FILE_TYPES:
            raise MessageProblem(FILES)
        count += 1
        if count > MAX_BLOCKS or depth > MAX_DEPTH:
            raise MessageProblem(TOO_LONG)
        children = block.get("children")
        if children is None:
            continue
        if not isinstance(children, list):
            raise MessageProblem(SHAPE)
        stack.extend((c, depth + 1) for c in children)


def check_body(body: Any) -> list[dict[str, Any]]:
    """`body` as it may be stored and posted, else `MessageProblem`."""
    if not isinstance(body, list):
        raise MessageProblem(SHAPE)
    if not body:
        raise MessageProblem(EMPTY)
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise MessageProblem(SHAPE) from None
    if size > MAX_BODY_BYTES:
        raise MessageProblem(TOO_LONG)
    _check_blocks(body)
    if not any(_block_has_content(b) for b in body):
        raise MessageProblem(EMPTY)
    return body


def preview(body: Any) -> str:
    """The first line of the message that says something, cut short: the posted message's
    `body_text` (what the chat list and a notification show) and the schedule's own preview.
    A line is read as a hand send reads it (`generate_first_line`); blank blocks before it are
    skipped, and a block that reader cannot name (a # chip alone) falls back to its text."""
    for block in body if isinstance(body, list) else []:
        line = generate_first_line.get(block) or extract_text([block])
        line = " ".join(line.split())
        if line:
            return line if len(line) <= PREVIEW_CHARS else line[: PREVIEW_CHARS - 1].rstrip() + "…"
    return ""


def with_fresh_ids(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of `body` whose blocks have new ids: every run posts a new message, and no two
    messages a person sends share a block id."""
    out: list[dict[str, Any]] = []
    for block in body:
        copy = {**block, "id": str(uuid.uuid4())}
        if isinstance(block.get("children"), list):
            copy["children"] = with_fresh_ids(block["children"])
        out.append(copy)
    return out

"""A note's STORED body as blocks: ids, text, a hash, and the same three edits collab makes (MIP-1646).

A note nobody has ever opened has no live doc in genos-collab, so `body` is the whole truth about it
and there is no editor to disturb. Reading its blocks and editing one then happens here, on the stored
JSON, with the same shapes collab answers with, so a caller cannot tell which side served it.

**A block with no stored id still has one here (MIP-1672).** A body written from markdown (`create_note`,
`update_note`) carries no block ids until the editor first loads it, so a note created over MCP and never
opened lists `block_id: null` and nothing could be edited by id. `effective_ids` gives every such block an id
DERIVED from its own content (type, props, content) plus how many identical blocks came before it: nothing is
written, every existing note is covered at once, an unchanged block keeps its id when others are inserted or
removed around it, and two identical paragraphs still get two ids. When the editor later opens the note it
assigns its own ids, and a derived id then answers "block not found, read again", like any stale id. A block
that HAS an id keeps it, and a block replaced here is stored with its id, so that one keeps working.

The hash is of the block as stored (type, props, content, children, id), canonical JSON. It is NOT
interchangeable with collab's hash of the same block (collab hashes the block as its reader returns it);
a hash read from one side and presented to the other is refused as stale, which fails safe: read again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

MAX_TEXT_CHARS = 500


# Fixed once: changing it would change every derived id (and so every id a caller is holding).
_DERIVED_NAMESPACE = uuid.UUID("5d0c3c0e-4f6e-5a52-9a3e-6b1f0f6c1d11")


def _identity(block: dict[str, Any]) -> str:
    """What makes a block "the same block" for deriving its id: its kind and its own content, never its
    children (they are blocks of their own) and never its position."""
    return json.dumps(
        [block.get("type"), block.get("props") or {}, block.get("content")],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def effective_ids(body: Any) -> dict[int, str]:
    """`id(block) -> its id` for every block of `body`, children included, in document order: the stored
    id when it has one, else the derived one. Keyed by object identity, so a caller that located a block in
    this very body can ask for its id (and `splice`, which works on a copy, computes its own)."""
    out: dict[int, str] = {}
    seen: dict[str, int] = {}
    taken = set(_ids(body)) if isinstance(body, list) else set()

    def walk(blocks: list[Any]) -> None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("id"):
                out[id(block)] = block["id"]
            else:
                key = _identity(block)
                nth = seen.get(key, 0)
                seen[key] = nth + 1
                derived = str(uuid.uuid5(_DERIVED_NAMESPACE, f"{key}#{nth}"))
                # Never the id another block already holds (a stored id equal to a derived one is
                # astronomically unlikely, but a duplicate id would make an edit ambiguous).
                while derived in taken:
                    derived = str(uuid.uuid5(_DERIVED_NAMESPACE, f"{derived}+"))
                taken.add(derived)
                out[id(block)] = derived
            children = block.get("children")
            if isinstance(children, list):
                walk(children)

    walk(body if isinstance(body, list) else [])
    return out


def block_hash(block: dict[str, Any]) -> str:
    canonical = json.dumps(block, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def inline_text(content: Any) -> str:
    """The words of one block's `content`: runs, links' runs, table cells, and a readable stand-in
    for the chips (which carry no text of their own). Only used to SHOW a block."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(inline_text(item) for item in content)
    if not isinstance(content, dict):
        return ""
    if content.get("type") == "tableContent":
        rows = content.get("rows") or []
        return "\n".join(
            " | ".join(inline_text(cell) for cell in (row.get("cells") or [])) for row in rows
        )
    if isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content.get("content"), list):
        return inline_text(content["content"])
    props = content.get("props") or {}
    kind = content.get("type")
    if kind == "mention":
        return f"@{props.get('userName', '')}"
    if kind == "mentionGroup":
        return f"@{props.get('groupName', '')}"
    if kind == "customEmoji":
        return f":{props.get('name', '')}:"
    if kind == "hashTask":
        return f"#{props.get('displayId') or props.get('title') or ''}"
    if kind in ("hashNote", "hashChat", "hashProject"):
        return f"#{props.get('title') or props.get('chatName') or props.get('projectName') or ''}"
    return ""


def summarize(body: Any) -> list[dict[str, Any]]:
    """Every block, children included, flat and in document order, in the shape `get_note` returns."""
    out: list[dict[str, Any]] = []
    ids = effective_ids(body)

    def walk(blocks: list[Any], depth: int, parent: str | None) -> None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = " ".join(inline_text(block.get("content")).split())
            children = block.get("children") if isinstance(block.get("children"), list) else []
            out.append(
                {
                    "block_id": ids[id(block)],
                    "type": block.get("type"),
                    "depth": depth,
                    "parent_block_id": parent,
                    "text": text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…",
                    "block_hash": block_hash(block),
                    "has_children": bool(children),
                }
            )
            walk(children, depth + 1, ids[id(block)])

    walk(body if isinstance(body, list) else [], 0, None)
    return out


def _locate(blocks: list[Any], block_id: str, ids: dict[int, str]) -> tuple[list[Any], int] | None:
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        if ids.get(id(block)) == block_id:
            return blocks, index
        children = block.get("children")
        if isinstance(children, list):
            hit = _locate(children, block_id, ids)
            if hit:
                return hit
    return None


def find_block(body: Any, block_id: str) -> dict[str, Any] | None:
    hit = _locate(body, block_id, effective_ids(body)) if isinstance(body, list) else None
    return hit[0][hit[1]] if hit else None


def _fresh(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The new blocks with ids of our own making: a caller-supplied id never reaches the note."""
    out = []
    for block in blocks:
        clean = {k: v for k, v in block.items() if k != "id"}
        clean["id"] = str(uuid.uuid4())
        if isinstance(clean.get("children"), list):
            clean["children"] = _fresh(clean["children"])
        out.append(clean)
    return out


def _ids(blocks: list[Any]) -> list[str]:
    found: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("id"):
                found.append(block["id"])
            if isinstance(block.get("children"), list):
                found.extend(_ids(block["children"]))
    return found


def splice(
    body: Any, block_id: str, op: str, new_blocks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """`(new body, ids of the new blocks)`, or None when `block_id` is not in the body.

    The same three edits collab makes: `replace` (the block and its children become `new_blocks`,
    the first keeping the replaced block's id so a link to it still opens), `insert_after` (they
    become its next siblings) and `delete`. A top level left empty gets one empty paragraph, as in
    the editor; a list item that lost its last child just has no children. The input is not changed.
    """
    working = copy.deepcopy(body) if isinstance(body, list) else []
    hit = _locate(working, block_id, effective_ids(working))
    if hit is None:
        return None
    siblings, index = hit
    fresh = _fresh(new_blocks) if op != "delete" else []
    new_ids: list[str] = []
    if op == "replace":
        fresh[0]["id"] = block_id
        new_ids = [i for i in _ids(fresh) if i != block_id]
        siblings[index : index + 1] = fresh
    elif op == "insert_after":
        new_ids = _ids(fresh)
        siblings[index + 1 : index + 1] = fresh
    else:
        del siblings[index]
    if not working:
        working = [
            {
                "id": str(uuid.uuid4()),
                "type": "paragraph",
                "props": {},
                "content": [],
                "children": [],
            }
        ]
    return working, new_ids

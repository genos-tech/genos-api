"""Plain-text extraction from BlockNote-style JSONField bodies.

Message bodies, comment bodies, task content, and note bodies in this
app are stored as BlockNote-style structured JSON. We need a plain
string for both keyword indexing and embedding generation.

Body shape: a list of blocks, where each block looks roughly like:

    {
      "type": "paragraph" | "heading" | "bulletListItem" | ...,
      "content": [
        {"type": "text", "text": "hello"},
        {"type": "mention", "props": {"userName": "alice"}},
        {"type": "link", "content": [{"text": "https://..."}]},
        ...
      ],
      "children": [<nested blocks>]  # optional
    }

Some entries may also be a single dict (not wrapped in a list) or a
plain string from older or simpler writers. We accept all of these.
"""

from typing import Any


def extract_text(body: Any) -> str:
    """Best-effort plain-text extraction from a BlockNote-style body.

    Never raises: unknown shapes degrade to an empty string rather
    than failing the entire indexing run.
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, list):
        return _join(_walk_blocks(body))
    if isinstance(body, dict):
        # Could be either a single block or a top-level wrapper.
        if "content" in body and isinstance(body.get("content"), list):
            return _join(_walk_block(body))
        # Some writers may nest the list under "blocks" or similar.
        for key in ("blocks", "body", "doc"):
            inner = body.get(key)
            if isinstance(inner, list):
                return _join(_walk_blocks(inner))
        return ""
    return ""


def _walk_blocks(blocks):
    parts = []
    for block in blocks:
        if isinstance(block, dict):
            parts.extend(_walk_block(block))
    return parts


def _walk_block(block):
    parts = []
    for inline in block.get("content", []) or []:
        parts.extend(_walk_inline(inline))
    # Recurse into nested children (e.g., list items with sub-lists).
    for child in block.get("children", []) or []:
        if isinstance(child, dict):
            parts.extend(_walk_block(child))
    return parts


def _walk_inline(inline):
    if not isinstance(inline, dict):
        return []
    t = inline.get("type")
    if t == "text":
        text = inline.get("text", "")
        return [str(text)] if text else []
    if t == "mention":
        props = inline.get("props") or {}
        name = props.get("userName") or props.get("name")
        return [f"@{name}"] if name else []
    if t == "link":
        # Links embed their own content array.
        nested = inline.get("content") or []
        return _join_inline(nested)
    # Unknown inline type — try to recover any "text" field.
    text = inline.get("text")
    return [str(text)] if text else []


def _join_inline(inlines):
    parts = []
    for inline in inlines:
        if isinstance(inline, dict):
            parts.extend(_walk_inline(inline))
    return parts


def _join(parts):
    return " ".join(p for p in parts if p).strip()

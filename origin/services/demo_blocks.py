"""BlockNote body builders shared by the demo/starter seeders.

Pure text → BlockNote JSON. No model imports, no language assumptions:
these take whatever strings they are handed and emit blocks, so Japanese
and English content flow through the same functions unchanged.

Extracted from `demo_seeder.py` when the demo content went bilingual —
the per-language content modules (`demo_content_en`, `demo_content_ja`)
both import from here, and `demo_seeder` re-exports the names that
`starter_seeder` / `guide_notes` have always imported from it.
"""

from __future__ import annotations

import uuid

# `isToggleable` ships on every heading the frontend writes; include it
# so seeded blocks match real ones byte-for-byte under diff tools.
_HEADING_PROPS = {
    "level": 3,
    "textColor": "default",
    "isToggleable": False,
    "textAlignment": "left",
    "backgroundColor": "default",
}
_PARA_PROPS = {
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}
_PLACEHOLDER_STYLES = {"italic": True, "textColor": "gray"}


def _block_id() -> str:
    """Fresh per-block UUID. BlockNote assigns one to every block in
    real user-typed documents; the seeded shape was missing this field,
    making the rows easy to spot as "not user-typed."""
    return str(uuid.uuid4())


def _heading(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "heading",
        "props": _HEADING_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _para(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _placeholder(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": _PLACEHOLDER_STYLES}],
        "children": [],
    }


def _blank_para() -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [],
        "children": [],
    }


def _bullet(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "bulletListItem",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _bullet_placeholder(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "bulletListItem",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": _PLACEHOLDER_STYLES}],
        "children": [],
    }


def _section(title: str, *body: dict) -> list:
    return [_heading(title), *body, _blank_para()]


def _text_body(text: str) -> list:
    """Single-paragraph chat message body. The chat preview renderer
    calls `content.slice(0, -1)` and requires the remainder to be
    non-empty, so we always emit a trailing empty paragraph."""
    return [_para(text), _blank_para()]


def _body(*sections):
    """Build a multi-section rich body. Each section is a tuple
    `(heading, [items])`. Items are either strings (paragraphs) or
    `("bullet", text)` / `("placeholder", text)` tuples."""
    blocks: list = []
    for heading_text, items in sections:
        blocks.append(_heading(heading_text))
        for item in items:
            if isinstance(item, tuple):
                kind, text = item
                if kind == "bullet":
                    blocks.append(_bullet(text))
                elif kind == "bullet_placeholder":
                    blocks.append(_bullet_placeholder(text))
                elif kind == "placeholder":
                    blocks.append(_placeholder(text))
                else:
                    blocks.append(_para(text))
            else:
                blocks.append(_para(item))
        blocks.append(_blank_para())
    return blocks

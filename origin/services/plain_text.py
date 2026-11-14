"""The text of a rich body as the timeline, the item lines and the briefing tell it (MIP-1475).

A `#` task chip carries a label (its display id and the title stored in the chip) that somebody
could have written, and `extract_text` (built for the search index) renders that label into the
text. A snippet a model reads must never copy it: the chip is told as `#<numeric target id>`, the
one thing that names the task. So every snippet of a comment, a note version or a message goes
through `body_text` / `message_text` here, not through `extract_text` and not through a message's
stored `body_text` (derived by the client from the body, label and all).

`extract_text` itself is left as it is: the search index and the fetch tools read it.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.text_extraction import extract_text


def chip_task_ids(body: Any) -> set[int]:
    """Every task a body points at with a `#` task chip (by `taskId`), at any depth."""
    found: set[int] = set()
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "hashTask":
                props = node.get("props")
                raw = props.get("taskId") if isinstance(props, dict) else None
                if isinstance(raw, bool):
                    raw = None
                if isinstance(raw, int) or (
                    isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) < 19
                ):
                    found.add(int(raw))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def plain_body(node: Any, depth: int = 0) -> Any:
    """A rich-text body with every `#` task chip turned into `#<numeric target id>`: the chip's own
    label (its display id, its stored title) is text somebody could have written and is never
    copied."""
    if depth > 40:
        return None
    if isinstance(node, list):
        return [plain_body(n, depth + 1) for n in node]
    if isinstance(node, dict):
        if node.get("type") == "hashTask":
            ids = chip_task_ids(node)
            return {"type": "text", "text": f"#{min(ids)}" if ids else "#task", "styles": {}}
        return {k: plain_body(v, depth + 1) for k, v in node.items()}
    return node


def body_text(body: Any) -> str:
    """The plain text of a rich body, with no chip label in it."""
    return extract_text(plain_body(body))


def message_text(message: Any) -> str:
    """A message's text, from its body when the body says anything, else its stored `body_text`
    (a message written without a rich body)."""
    return body_text(message.body) or message.body_text or ""

"""BlockNote → Markdown rendering for agent-read entity bodies.

The inverse of `blocknote_md.markdown_to_blocks`. That module exists so
the agent can *write* structure; this one exists so it can *read* it.

Until now the only way a tool could surface a body was
`text_extraction.extract_text`, a flattener built for the search index:
it walks every `content`/`children` array and joins the text runs. That
is right for embeddings — structure is noise to a vector — and wrong for
anything that has to act on the body. A task description written as

    ## Acceptance criteria
    - [ ] returns 404 for a foreign team
    - [x] covered by a test

flattens to one undifferentiated line: the heading stops being a
heading, the checkboxes vanish, and a reader cannot tell which items are
already done. Consumers that hand a task spec to an external coding
agent (the MCP surface) need the markdown back.

Scope is deliberately the same small vocabulary `markdown_to_blocks`
emits — heading / paragraph / list items / links / bold / italic — plus
the block types the *editor* can produce that it cannot: checklists,
code blocks, quotes, tables, and media. Anything unrecognised degrades
to its flattened text rather than disappearing, so this is never lossier
than `extract_text` was.

Like `extract_text`, this never raises: an unknown or malformed shape
yields the best text available (or `""`), because the callers are read
paths where a body that renders badly beats a request that 500s.
"""

from __future__ import annotations

import re
from typing import Any

# The body-shape zoo (bare list / {"content": …} / {"blocks": …} / a
# plain string from older writers) is normalised in exactly one place.
# Importing it keeps that single source of truth rather than growing a
# second copy here that drifts the first time a writer invents a shape.
from origin.search_engine.text_extraction import _top_level_blocks, extract_text

# Blocks that form a tight run — consecutive items are one markdown list,
# not separate paragraphs.
_LIST_TYPES = frozenset({"bulletListItem", "numberedListItem", "checkListItem"})

# Media blocks render as a link/image using whatever the editor stored.
_MEDIA_TYPES = frozenset({"image", "video", "audio", "file"})

_INDENT = "  "


# Emphasis styles, in wrapping order (innermost first). Bold outside
# italic is the conventional nesting.
_EMPHASIS = (("strike", "~~"), ("italic", "*"), ("bold", "**"))


def _wrap(text: str, emphasis: frozenset[str]) -> str:
    if not text:
        return ""
    for style, marker in _EMPHASIS:
        if style in emphasis:
            text = f"{marker}{text}{marker}"
    return text


def _emphasis_of(node: dict[str, Any], strip: frozenset[str]) -> frozenset[str]:
    """The emphasis a node carries beyond `strip` (the styles an
    enclosing run already wraps). A link carries what every run of its
    label shares, so `**see [docs](u)**` wraps the link once."""
    ntype = node.get("type")
    if ntype == "text":
        styles = node.get("styles")
        if not isinstance(styles, dict):
            return frozenset()
        return frozenset(s for s, _ in _EMPHASIS if styles.get(s)) - strip
    if ntype == "link":
        runs = node.get("content")
        if not isinstance(runs, list) or not runs:
            return frozenset()
        shared = [_emphasis_of(r, strip) if isinstance(r, dict) else frozenset() for r in runs]
        return frozenset.intersection(*shared)
    return frozenset()


def _styled(text: str, styles: dict[str, Any]) -> str:
    """One text run, in a code span if it is code. Emphasis is wrapped
    by `_inline`, outside the span — ``**`x`**`` renders (and
    `markdown_to_blocks` reads it back as bold code), but `` `**x**` ``
    would show literal asterisks inside it.
    """
    if styles.get("code"):
        # The fence must be longer than any backtick run inside, and
        # padded when the text starts/ends with one (CommonMark), or a
        # span holding a backtick re-parses as a different span.
        longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * (longest + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        return f"{fence}{pad}{text}{pad}{fence}"
    return text


def _inline(nodes: Any, strip: frozenset[str] = frozenset()) -> str:
    """Render one block's inline `content` array to markdown.

    Neighbouring runs with the same emphasis are wrapped once, as a
    group: per-run wrapping turns bold code followed by bold text into
    ``**`x`**** y**``, whose middle `****` re-parses as literal
    asterisks — and agents edit bodies by re-saving this rendering.
    """
    if isinstance(nodes, str):
        return nodes
    if not isinstance(nodes, list):
        return ""
    out: list[str] = []
    group: list[str] = []
    group_emphasis: frozenset[str] = frozenset()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        emphasis = _emphasis_of(node, strip)
        rendered = _inline_node(node, strip | emphasis)
        if not rendered:
            continue  # an empty run must not split a group into `**a****b**`
        if emphasis != group_emphasis:
            out.append(_wrap("".join(group), group_emphasis))
            group, group_emphasis = [], emphasis
        group.append(rendered)
    out.append(_wrap("".join(group), group_emphasis))
    return "".join(out)


def _inline_node(node: dict[str, Any], strip: frozenset[str]) -> str:
    ntype = node.get("type")
    if ntype == "text":
        styles = node.get("styles")
        text = str(node.get("text") or "")
        return _styled(text, styles) if text and isinstance(styles, dict) else text
    if ntype == "link":
        label = _inline(node.get("content"), strip) or str(node.get("href") or "")
        href = str(node.get("href") or "")
        return f"[{label}]({href})" if href else label
    if ntype == "mention":
        props = node.get("props") or {}
        name = props.get("userName") or props.get("name")
        return f"@{name}" if name else ""
    if ntype == "customEmoji":
        name = (node.get("props") or {}).get("name")
        return f":{name}:" if name else ""
    # Unknown inline type — recover any text it carries rather than
    # dropping the run (mirrors _walk_inline's fallback).
    text = node.get("text")
    return str(text) if text else ""


def _plain(nodes: Any) -> str:
    """Inline content with no markers — for code blocks, where markdown
    syntax inside the source must stay literal."""
    if isinstance(nodes, str):
        return nodes
    if not isinstance(nodes, list):
        return ""
    return "".join(
        str(n.get("text") or "") for n in nodes if isinstance(n, dict) and n.get("type") == "text"
    )


def _is_checked(props: dict[str, Any]) -> bool:
    """BlockNote has stored `checked` as both a bool and the string
    "true" over its lifetime; the frontend's renderer accepts either, so
    this does too."""
    checked = props.get("checked")
    return checked is True or checked == "true"


def _render_table(block: dict[str, Any]) -> str:
    """Best-effort pipe table. Falls back to flattened text when the
    shape isn't the `{"rows": [{"cells": [[inline…], …]}]}` we know."""
    content = block.get("content")
    rows = content.get("rows") if isinstance(content, dict) else None
    if not isinstance(rows, list) or not rows:
        return _inline(block.get("content"))
    rendered: list[list[str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cells = row.get("cells")
        if not isinstance(cells, list):
            continue
        # A cell is an inline array; newer BlockNote wraps it in a dict.
        rendered.append(
            [
                _inline(cell.get("content") if isinstance(cell, dict) else cell).replace("|", "\\|")
                for cell in cells
            ]
        )
    if not rendered:
        return ""
    width = max(len(r) for r in rendered)
    lines = []
    for i, row in enumerate(rendered):
        padded = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if i == 0:
            # The editor has no header concept, but markdown requires a
            # separator for the table to render at all — treat row 0 as
            # the header, which is what the UI displays anyway.
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _render_media(block: dict[str, Any], btype: str) -> str:
    props = block.get("props") or {}
    url = str(props.get("url") or "")
    label = str(props.get("caption") or props.get("name") or btype)
    if not url:
        return label if label != btype else ""
    return f"![{label}]({url})" if btype == "image" else f"[{label}]({url})"


def _render_leaf(block: dict[str, Any], depth: int) -> str:
    """One non-list block, indented for its nesting depth."""
    indent = _INDENT * depth
    btype = str(block.get("type") or "")
    props = block.get("props") if isinstance(block.get("props"), dict) else {}

    if btype == "heading":
        try:
            level = int(props.get("level") or 1)
        except (TypeError, ValueError):
            level = 1
        level = min(max(level, 1), 6)
        text = _inline(block.get("content"))
        return f"{indent}{'#' * level} {text}" if text else ""

    if btype == "codeBlock":
        language = str(props.get("language") or "")
        source = _plain(block.get("content"))
        body = "\n".join(f"{indent}{line}" for line in source.split("\n")) if indent else source
        return f"{indent}```{language}\n{body}\n{indent}```"

    if btype == "quote":
        text = _inline(block.get("content"))
        return "\n".join(f"{indent}> {line}" for line in text.split("\n")) if text else ""

    if btype == "table":
        table = _render_table(block)
        if not table:
            return ""
        return "\n".join(f"{indent}{line}" for line in table.split("\n")) if indent else table

    if btype in _MEDIA_TYPES:
        media = _render_media(block, btype)
        return f"{indent}{media}" if media else ""

    if btype == "divider":
        return f"{indent}---"

    # paragraph and anything unrecognised: render whatever inline content
    # it has. Never drop the block.
    text = _inline(block.get("content"))
    return f"{indent}{text}" if text else ""


def _render_list_item(block: dict[str, Any], depth: int, number: int) -> list[str]:
    indent = _INDENT * depth
    btype = block.get("type")
    text = _inline(block.get("content"))
    if btype == "numberedListItem":
        marker = f"{number}. "
    elif btype == "checkListItem":
        props = block.get("props") if isinstance(block.get("props"), dict) else {}
        marker = "- [x] " if _is_checked(props) else "- [ ] "
    else:
        marker = "- "
    lines = [f"{indent}{marker}{text}"]
    # Children stay inside the same list part so the run isn't broken by
    # a blank line, which would end the list in most markdown parsers.
    for part in _render_blocks(block.get("children") or [], depth + 1):
        lines.extend(part.split("\n"))
    return lines


def _render_blocks(blocks: Any, depth: int = 0) -> list[str]:
    """Render blocks into markdown *parts* — each part is one logical
    chunk, later joined by blank lines. Consecutive list items collapse
    into a single part so the list stays tight."""
    if not isinstance(blocks, list):
        return []
    parts: list[str] = []
    pending: list[str] = []
    number = 0

    def flush() -> None:
        nonlocal pending, number
        if pending:
            parts.append("\n".join(pending))
            pending = []
        number = 0

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")

        if btype in _LIST_TYPES:
            number = number + 1 if btype == "numberedListItem" else 0
            pending.extend(_render_list_item(block, depth, number))
            continue

        flush()
        leaf = _render_leaf(block, depth)
        if leaf:
            parts.append(leaf)
        # A non-list block can still nest (toggle blocks, table cells in
        # some versions) — render children as siblings at one more depth.
        parts.extend(_render_blocks(block.get("children") or [], depth + 1))

    flush()
    return parts


def blocks_to_markdown(body: Any) -> str:
    """Render a BlockNote body to markdown.

    Never raises — an unknown shape degrades to `extract_text`'s plain
    output, and a wholly unparseable one to `""`.
    """
    try:
        blocks = _top_level_blocks(body)
        if not blocks:
            # Plain strings and shapes we don't recognise: extract_text
            # already handles both, and its answer beats an empty one.
            return extract_text(body)
        return "\n\n".join(_render_blocks(blocks)).strip()
    except Exception:  # noqa: BLE001 - read path; a bad body must not 500
        return extract_text(body)

"""Markdown → BlockNote block conversion for agent-written bodies.

The agent writes markdown. When it saves that into a note or a task
body, the body must be BlockNote-shaped JSON so the editor renders the
structure natively instead of showing one wall of plain text with
literal `##`/`**`/`|` control characters.

Vocabulary: headings, bullet / numbered / check lists (nested by
indentation), quotes, fenced code blocks, pipe tables, dividers, and
inline **bold**, *italic*, ~~strike~~, `code` and links. Block and
inline shapes are the ones BlockNote 0.49's own `tryParseMarkdownToBlocks`
produces for the same input (the frontend's note import uses that
parser), so a body the agent writes is indistinguishable from one a user
imported or typed. `blocknote_render.blocks_to_markdown` is the inverse.

Citation tokens (`[prose](type:id)` and bare `[type:id]`)
are resolved into in-app `/workspace/...` links when the caller supplies
an `entity_link_resolver` (see `entity_links.resolve_note_entity_link`;
create_note / update_note pass one bound to the caller's team); without
a resolver, or on a miss, the target degrades to its visible prose
(never a dead link).

The output is consumed by two places, both of which this shape satisfies:
  * the BlockNote editor (default block types with the standard props;
    the task/note schemas keep every default block), and
  * the reindex chunker (`text_extraction.extract_sections`), which walks
    `content`/`children` of every block type — table cells included —
    and splits on `heading` blocks, so structured output actually
    *improves* search precision (heading-bounded sections).
"""

from __future__ import annotations

import re
import uuid
from bisect import bisect_left
from typing import Any, Callable

# Maps a citation token ("task:12") to (href, fallback_label), or None
# when it can't be resolved (unknown id, foreign team, unsupported type).
EntityLinkResolver = Callable[[str], "tuple[str, str] | None"]

_BASE_PROPS = {
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}

# Inline markers. Bold is matched before italic so `**x**` isn't eaten by
# the single-`*` italic rule. Underscore italic/bold use word-boundary
# guards so `snake_case_name` isn't mangled into italics. Mirrors the
# frontend regex vocabulary.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_FORMAT_RE = re.compile(
    r"\*\*(.+?)\*\*"  # **bold**
    r"|__(.+?)__"  # __bold__
    r"|(?<![A-Za-z0-9])\*([^*\s][^*]*?)\*(?![A-Za-z0-9])"  # *italic*
    r"|(?<![A-Za-z0-9])_([^_\s][^_]*?)_(?![A-Za-z0-9])"  # _italic_
    r"|~~(.+?)~~"  # ~~strike~~
)
# Code spans are cut out before any other inline rule runs: their text is
# literal, so `` `**x**` `` or `` `[a](b)` `` must not become bold or a link.
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")

# Accept 1–6 `#` (markdown allows h1–h6) but cap the rendered level at 3,
# which is what BlockNote's default theme supports — a `#### Sub` heading
# renders as level 3 rather than leaking literal `####` into a paragraph.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# Checklist before bullet: `- [ ] x` is also a valid bullet.
_CHECK_RE = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+•]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([^`\s]*)")
_DIVIDER_RE = re.compile(r"^(?:-\s*){3,}$|^(?:\*\s*){3,}$|^(?:_\s*){3,}$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$")

# Fence info strings → the language ids the editor's syntax-highlighted
# code block (`@blocknote/code-block`'s `supportedLanguages`) knows. Its
# editor-side ``` shortcut resolves aliases the same way
# (`getLanguageId`), so `py` lands as `python` either way. Anything not
# listed is kept as written — the editor shows it unhighlighted.
_LANGUAGE_ALIASES = {
    "": "text",
    "txt": "text",
    "plain": "text",
    "plaintext": "text",
    "c++": "cpp",
    "gql": "graphql",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "sh": "shellscript",
    "bash": "shellscript",
    "shell": "shellscript",
    "zsh": "shellscript",
    "yml": "yaml",
    "md": "markdown",
    "rb": "ruby",
    "rs": "rust",
    "kt": "kotlin",
    "cs": "csharp",
    "c#": "csharp",
    "hs": "haskell",
    "objc": "objective-c",
    "tex": "latex",
}

# A nested list item is indented at least this far past its parent's
# marker column (CommonMark accepts 2 for `-`, and 2–4 is what models emit).
_MIN_NEST_INDENT = 2


def _text_node(text: str, styles: dict[str, bool] | None = None) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": styles or {}}


def _format_spans(text: str) -> list[tuple[int, int, dict[str, bool]]]:
    """`(start, end, styles)` for each run of `text` once the
    bold/italic/strike markers are dropped; unmarked runs carry `{}`."""
    spans: list[tuple[int, int, dict[str, bool]]] = []
    cursor = 0
    for m in _FORMAT_RE.finditer(text):
        if m.start() > cursor:
            spans.append((cursor, m.start(), {}))
        group = next(g for g in range(1, 6) if m.group(g) is not None)
        style = "bold" if group in (1, 2) else "strike" if group == 5 else "italic"
        spans.append((m.start(group), m.end(group), {style: True}))
        cursor = m.end()
    if cursor < len(text):
        spans.append((cursor, len(text), {}))
    return spans


def _tokenize_formatting(text: str) -> list[dict[str, Any]]:
    """Split a code- and link-free run into text nodes carrying
    bold/italic/strike styles."""
    return [_text_node(text[s:e], dict(styles)) for s, e, styles in _format_spans(text)]


# Bare citation token in prose — `[task:12]` — that is NOT the label of
# a markdown link (no following paren). Same vocabulary as the frontend's
# CITATION_PATTERN.
_BARE_TOKEN_RE = re.compile(r"\[((?:chat|task|note|project|todo|milestone):[^\]\s]+)\](?!\()")

# Stands in for an atom (code span, link) in the line handed to the
# emphasis pass, so `**`x` y**` and `**see [docs](u)**` still pair their
# markers across it. Atoms are found again by offset, never by this
# character, so a literal U+FFFC in the text stays text.
_ATOM = "\ufffc"

# One line mid-tokenising: literal text, or an atom's finished nodes.
_Parts = list["str | list[dict[str, Any]]"]


def _link_node(href: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "link", "href": href, "content": content}


def _with_styles(node: dict[str, Any], styles: dict[str, bool]) -> dict[str, Any]:
    """An atom node under enclosing emphasis: a code run keeps `code` and
    gains the style; a link passes it to every run of its label."""
    if not styles:
        return node
    if node["type"] == "link":
        return {**node, "content": [_with_styles(c, styles) for c in node["content"]]}
    return {**node, "styles": {**node["styles"], **styles}}


def _tokenize_inline(text: str, resolver: EntityLinkResolver | None = None) -> list[dict[str, Any]]:
    """Inline content for one line. Code spans are cut out first, since
    their text is literal, then links and citation tokens; each becomes
    an atom, and emphasis is matched over the whole line with the atoms
    held in place, so a marker pair wrapping one styles it too."""
    if not text:
        return []
    parts: _Parts = []
    cursor = 0
    for m in _CODE_SPAN_RE.finditer(text):
        _collect_links(text[cursor : m.start()], resolver, parts)
        # CommonMark strips one space each side so `` `` `x` `` `` can
        # hold a backtick; ordinary spans are unaffected.
        code = m.group(2)
        if len(code) > 2 and code.startswith(" ") and code.endswith(" "):
            code = code[1:-1]
        parts.append([_text_node(code, {"code": True})])
        cursor = m.end()
    _collect_links(text[cursor:], resolver, parts)

    line: list[str] = []
    atoms: dict[int, list[dict[str, Any]]] = {}
    offset = 0
    for part in parts:
        if isinstance(part, str):
            line.append(part)
            offset += len(part)
        else:
            atoms[offset] = part
            line.append(_ATOM)
            offset += 1
    run = "".join(line)
    atom_offsets = sorted(atoms)

    out: list[dict[str, Any]] = []
    for start, end, styles in _format_spans(run):
        cursor = start
        for pos in atom_offsets[bisect_left(atom_offsets, start) : bisect_left(atom_offsets, end)]:
            if pos > cursor:
                out.append(_text_node(run[cursor:pos], dict(styles)))
            out.extend(_with_styles(node, styles) for node in atoms[pos])
            cursor = pos + 1
        if end > cursor:
            out.append(_text_node(run[cursor:end], dict(styles)))
    return out


def _collect_links(text: str, resolver: EntityLinkResolver | None, parts: _Parts) -> None:
    """A code-free run: markdown links become BlockNote link
    nodes — real URLs always, and `[prose](task:5)`-style citation
    targets when an entity-link resolver is supplied (create_note /
    update_note pass one bound to the caller's team, so saved notes
    carry working in-app links). An unresolvable citation target
    degrades to its prose so the note never carries a dead link."""
    cursor = 0
    for m in _LINK_RE.finditer(text):
        _collect_citation_tokens(text[cursor : m.start()], resolver, parts)
        label, href = m.group(1), m.group(2)
        if href.startswith(("http://", "https://", "mailto:")):
            parts.append([_link_node(href, _tokenize_formatting(label))])
        else:
            resolved = resolver(href) if resolver else None
            if resolved:
                # Citation target → in-app link, keeping the model's prose.
                parts.append([_link_node(resolved[0], _tokenize_formatting(label))])
            else:
                # Non-URL target we can't resolve — keep the prose.
                parts.append(label)
        cursor = m.end()
    _collect_citation_tokens(text[cursor:], resolver, parts)


def _collect_citation_tokens(text: str, resolver: EntityLinkResolver | None, parts: _Parts) -> None:
    """A link-free run: resolve bare `[type:id]` citation tokens into
    links when a resolver is supplied (label = the entity's
    display_id/name/title), otherwise/on a miss keep the literal text."""
    if resolver is None:
        parts.append(text)
        return
    cursor = 0
    for m in _BARE_TOKEN_RE.finditer(text):
        resolved = resolver(m.group(1))
        if not resolved:
            continue  # leave the literal token inside the surrounding run
        parts.append(text[cursor : m.start()])
        # The entity's own name — literal, never emphasis-parsed.
        parts.append([_link_node(resolved[0], [_text_node(resolved[1])])])
        cursor = m.end()
    parts.append(text[cursor:])


def _block(
    block_type: str,
    text: str,
    *,
    level: int | None = None,
    resolver: EntityLinkResolver | None = None,
    extra_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    props = dict(_BASE_PROPS)
    if level is not None:
        props["level"] = level
    if extra_props:
        props.update(extra_props)
    return {
        "type": block_type,
        "props": props,
        "content": _tokenize_inline(text, resolver),
        "children": [],
    }


def _indent_of(line: str) -> int:
    """Leading-whitespace width, a tab counting as four columns."""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4
        else:
            break
    return width


def _split_row(line: str) -> list[str]:
    """Cells of one pipe-table row. `\\|` is a literal pipe, not a
    boundary; the optional outer pipes are dropped."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = re.split(r"(?<!\\)\|", row)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _is_table_start(lines: list[str], i: int) -> bool:
    """A header row followed by a `| --- |` separator. Requiring the
    separator keeps prose that merely contains a pipe a paragraph."""
    if i + 1 >= len(lines):
        return False
    header = lines[i].strip()
    sep = lines[i + 1].strip()
    if "|" not in header or "|" not in sep or not _TABLE_SEPARATOR_RE.match(sep):
        return False
    return len(_split_row(header)) == len(_split_row(sep))


def _table_block(rows: list[list[str]], resolver: EntityLinkResolver | None) -> dict[str, Any]:
    """BlockNote 0.49 table: row 0 is the header (`headerRows: 1`), and
    every row is padded/truncated to the header's width, which is how
    BlockNote's own parser treats a ragged GFM table."""
    width = len(rows[0])
    cell_props = {**_BASE_PROPS, "colspan": 1, "rowspan": 1}
    return {
        "type": "table",
        "props": {"textColor": "default"},
        "content": {
            "type": "tableContent",
            "columnWidths": [None] * width,
            "headerRows": 1,
            "rows": [
                {
                    "cells": [
                        {
                            "type": "tableCell",
                            "props": dict(cell_props),
                            "content": _tokenize_inline(cell, resolver),
                        }
                        for cell in (row + [""] * width)[:width]
                    ]
                }
                for row in rows
            ],
        },
        "children": [],
    }


def _code_block(language: str, source: str) -> dict[str, Any]:
    lang = language.strip().lower()
    return {
        "type": "codeBlock",
        "props": {"language": _LANGUAGE_ALIASES.get(lang, lang)},
        # Literal: no inline parsing inside code.
        "content": [_text_node(source)] if source else [],
        "children": [],
    }


def _divider_block() -> dict[str, Any]:
    return {"type": "divider", "props": {}, "children": []}


def _list_item(stripped: str) -> tuple[str, str, dict[str, Any]] | None:
    """(block_type, text, extra_props) when the line is a list item."""
    check = _CHECK_RE.match(stripped)
    if check:
        return "checkListItem", check.group(2), {"checked": check.group(1) != " "}
    bullet = _BULLET_RE.match(stripped)
    if bullet:
        return "bulletListItem", bullet.group(1), {}
    numbered = _NUMBERED_RE.match(stripped)
    if numbered:
        return "numberedListItem", numbered.group(1), {}
    return None


def _starts_block(stripped: str, lines: list[str], i: int) -> bool:
    """Would this line open a non-paragraph block? (Ends a paragraph.)"""
    return bool(
        _HEADING_RE.match(stripped)
        or _FENCE_RE.match(stripped)
        or _QUOTE_RE.match(stripped)
        or _DIVIDER_RE.match(stripped)
        or _list_item(stripped)
        or _is_table_start(lines, i)
    )


def markdown_to_blocks(
    markdown: str, *, entity_link_resolver: EntityLinkResolver | None = None
) -> list[dict[str, Any]]:
    """Parse the agent's markdown into BlockNote `PartialBlock`-shaped dicts.

    Handles headings (# … ######, capped at level 3), bullet / numbered /
    check lists nested by indentation, `>` quotes, fenced code blocks,
    pipe tables, `---` dividers and paragraphs, with inline **bold**,
    *italic*, ~~strike~~, `code` and markdown links. Anything
    unrecognised degrades to a plain paragraph rather than being dropped.

    Returns `[]` for empty input (a deliberately title-only note), matching
    the previous `_wrap_blocknote` contract.
    """
    text = (markdown or "").strip("\n")
    if not text.strip():
        return []

    resolver = entity_link_resolver
    lines = text.split("\n")
    blocks: list[dict[str, Any]] = []
    # Open list items as (marker_indent, block): a deeper-indented item
    # becomes a child of the nearest shallower one.
    list_stack: list[tuple[int, dict[str, Any]]] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        # Blank line — block boundary (but a list may continue after it).
        if not stripped:
            i += 1
            continue

        # Fenced code block: everything up to the matching fence is
        # literal, including lines that look like headings or lists. An
        # unclosed fence runs to the end, as in CommonMark.
        fence = _FENCE_RE.match(stripped)
        if fence:
            marker = fence.group(1)
            body: list[str] = []
            fence_indent = _indent_of(raw)
            i += 1
            while i < n:
                closing = lines[i].strip()
                if closing.startswith(marker[0] * len(marker)) and not closing.strip(marker[0]):
                    i += 1
                    break
                line = lines[i]
                # Drop the fence's own indentation from each line, so a
                # fence nested under a list item isn't shifted right.
                body.append(line[min(fence_indent, _indent_of(line)) :])
                i += 1
            list_stack.clear()
            blocks.append(_code_block(fence.group(2), "\n".join(body)))
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            list_stack.clear()
            blocks.append(_block("heading", heading.group(2), level=level, resolver=resolver))
            i += 1
            continue

        # Divider before lists: `- - -` and `* * *` are rules, not bullets.
        if _DIVIDER_RE.match(stripped):
            list_stack.clear()
            blocks.append(_divider_block())
            i += 1
            continue

        if _is_table_start(lines, i):
            rows = [_split_row(lines[i])]
            i += 2  # header + separator
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            list_stack.clear()
            blocks.append(_table_block(rows, resolver))
            continue

        quote = _QUOTE_RE.match(stripped)
        if quote:
            # Consecutive `>` lines are one quote; lines join with a
            # space, like BlockNote's parser.
            buf = [quote.group(1).strip()]
            i += 1
            while i < n:
                more = _QUOTE_RE.match(lines[i].strip())
                if not more:
                    break
                buf.append(more.group(1).strip())
                i += 1
            list_stack.clear()
            blocks.append(_block("quote", " ".join(p for p in buf if p), resolver=resolver))
            continue

        item = _list_item(stripped)
        if item:
            block_type, item_text, extra = item
            indent = _indent_of(raw)
            block = _block(block_type, item_text, resolver=resolver, extra_props=extra)
            while list_stack and indent < list_stack[-1][0] + _MIN_NEST_INDENT:
                list_stack.pop()
            if list_stack:
                list_stack[-1][1]["children"].append(block)
            else:
                blocks.append(block)
            list_stack.append((indent, block))
            i += 1
            continue

        # Paragraph: gather until a blank line or a block-starting line.
        list_stack.clear()
        buf = [stripped.rstrip("\\")]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if not nxt or _starts_block(nxt, lines, i):
                break
            buf.append(nxt.rstrip("\\"))
            i += 1
        blocks.append(_block("paragraph", " ".join(buf), resolver=resolver))

    return blocks


def task_body_blocks(markdown: str) -> list[dict[str, Any]]:
    """A task body written by a tool, shaped like one the editor saved.

    Every tool that writes `TaskMaster.content` goes through this, so the
    same text produces the same document whichever tool wrote it.
    `create_task` used to keep its own copy that split on newlines, which
    stored `###` and `- ` as literal text (MIP-691) while `update_task`
    parsed the same input as markdown.

    On top of `markdown_to_blocks` it adds the two things a user-typed body
    always has: a stable `id` on every top-level block, and the trailing
    blank paragraph the editor keeps as its last line. Empty input stays
    `[]` (a title-only task).
    """
    blocks = markdown_to_blocks((markdown or "").strip())
    if not blocks:
        return []
    for block in blocks:
        block.setdefault("id", str(uuid.uuid4()))
    return blocks + [{"id": str(uuid.uuid4()), **_block("paragraph", "")}]

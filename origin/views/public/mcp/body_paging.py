"""`get_note` and `get_task` read a long body in parts (MIP-1367, phase 2, behind `MCP_RESULT_LIMITS`).

A note can be 273,000 characters and a task body 262,000: far more than the model's context should
take in one answer. With the switch on, both reads take two more arguments:

* `offset`: where to start, in characters of the body as shown (`body_markdown` for a note,
  `content_markdown` for a task). Default 0.
* `max_chars`: how much to return at most. Default 20,000 (`MCP_BODY_PAGE_CHARS`), lowest 1,000;
  the answer still has to fit `MCP_RESULT_MAX_CHARS` with everything else in it, so the part can
  be shorter than asked.

The part ends at a paragraph break when there is one in its last 30 percent, else at a line break,
else at the character (`cut_point`). An answer that is not the whole body says so, in the
`truncated` notice and in two fields that are there only then: `body_total_chars` and, when more
is left, `next_offset`.

**A partial read can never be written back.** `get_task` returns a `body_hash` that `update_task`
takes as `base_body_hash` ("refuse my edit if the body changed since I read it"). It is returned
only for a COMPLETE read, offset 0 and the whole body: a hash handed out for a part would let a
caller say "I read the whole body" after reading a tenth of it. (`result_limits._with_notice` does
the removal, so a body cut by the generic guard loses it too.) `update_note` has no such hash; the
notice says "partial: do not write this back" and that is all that protects a note (see the PR).

A get_task body is read together with its newest comments. When the comments leave less than
`MIN_BODY_SHARE` characters for the body, the OLDEST comments are left out first (the newest are
what a reader acts on) and the notice says how many.
"""

from __future__ import annotations

from typing import Any, Callable

from origin.search_engine.agent.tools.base import ToolError
from origin.views.public.mcp.result_limits import (
    BODY_FIELDS,
    MCP_RESULT_MAX_CHARS,
    Cuts,
    _env_int,
)

MIN_PAGE_CHARS = 1_000
DEFAULT_PAGE_CHARS = _env_int("MCP_BODY_PAGE_CHARS", 20_000, MIN_PAGE_CHARS)
#: What a get_task body is guaranteed before comments are left out to make room.
MIN_BODY_SHARE = 8_000
#: Room kept for the `truncated` notice and the fields a partial read adds.
NOTICE_ROOM = 1_800
#: The part ends at a paragraph break found in this last share of its window.
BREAK_WINDOW = 0.3

#: The tool a body is edited with, for the sentence that says not to write a part back.
_WRITE_TOOL = {"get_task": "update_task", "get_note": "update_note"}
_WHAT = {"get_task": "task description", "get_note": "note"}

PAGE_ARGS = ("offset", "max_chars")


def schema_properties(tool: str) -> dict[str, dict[str, Any]]:
    """The two parameters, as published while the switch is on."""
    what = _WHAT[tool]
    field = BODY_FIELDS[tool][0]
    return {
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                f"Where to start reading the {what}, in characters of `{field}`. Default 0. A long "
                "body comes back in parts: pass the `next_offset` of the previous part."
            ),
        },
        "max_chars": {
            "type": "integer",
            "minimum": MIN_PAGE_CHARS,
            "description": (
                f"The most characters of `{field}` to return. Default {DEFAULT_PAGE_CHARS:,}. The "
                "answer is cut at a paragraph break, so a part can be shorter."
            ),
        },
    }


def _int_arg(args: dict[str, Any], name: str, default: int, low: int) -> int:
    value = args.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"`{name}` must be a whole number (got {value!r}).") from None
    if number < low:
        raise ToolError(f"`{name}` must be at least {low} (got {number}).")
    return number


def validate(args: dict[str, Any]) -> tuple[int, int]:
    """`(offset, max_chars)` from the call, or a `ToolError` in words a model can act on."""
    offset = _int_arg(args, "offset", 0, 0)
    max_chars = _int_arg(args, "max_chars", DEFAULT_PAGE_CHARS, MIN_PAGE_CHARS)
    return offset, max_chars


def cut_point(body: str, start: int, window: int) -> int:
    """Where the part that starts at `start` and may be `window` characters long ends: after a
    paragraph break in its last `BREAK_WINDOW`, else after a line break there, else at the limit."""
    limit = min(start + window, len(body))
    if limit >= len(body):
        return len(body)
    floor = start + int(window * (1 - BREAK_WINDOW))
    for needle in ("\n\n", "\n"):
        at = body.rfind(needle, floor, limit)
        if at != -1:
            return at + len(needle)
    return limit


def _fits_room(payload: dict[str, Any], field: str, render: Callable[[dict], str]) -> int:
    """Characters the answer takes without its body text."""
    held = payload[field]
    payload[field] = ""
    try:
        return len(render(payload))
    finally:
        payload[field] = held


def page_body(
    tool: str,
    payload: dict[str, Any],
    args: dict[str, Any],
    render: Callable[[dict], str],
    cuts: Cuts,
    cap: int | None = None,
) -> None:
    """Cut `payload`'s body to the part asked for, in place, and say so in `cuts` and in the
    fields a partial read carries. A complete read changes nothing."""
    cap = cap or MCP_RESULT_MAX_CHARS
    field = next((f for f in BODY_FIELDS[tool] if isinstance(payload.get(f), str)), None)
    if field is None:
        return
    offset, max_chars = validate(args)
    body = payload[field]
    total = len(body)
    if offset and offset >= total:
        raise ToolError(
            f"`offset` {offset} is past the end: the {_WHAT[tool]} has {total} characters. "
            "Pass the `next_offset` of the previous part."
        )

    comments = payload.get("comments") if tool == "get_task" else None
    comments_left_out = 0
    room = cap - _fits_room(payload, field, render) - NOTICE_ROOM
    wanted = min(max_chars, max(total - offset, 0))
    while isinstance(comments, list) and comments and room < min(wanted, MIN_BODY_SHARE):
        comments.pop(0)
        comments_left_out += 1
        room = cap - _fits_room(payload, field, render) - NOTICE_ROOM
    window = max(min(max_chars, room), 200)

    end = cut_point(body, offset, window) if offset or total > window else total
    if end - offset > window:
        end = offset + window
    if comments_left_out:
        cuts.drop("comments", comments_left_out)
    if offset == 0 and end >= total:
        if comments_left_out:
            cuts.has_more = False
            cuts.next_call = (
                f"{comments_left_out} older comment(s) are left out to make room for the "
                "description; they are in the Genos app."
            )
        return

    payload[field] = body[offset:end]
    payload["body_total_chars"] = total
    more = end < total
    if more:
        payload["next_offset"] = end
    cuts.field(field)
    cuts.has_more = more
    write = _WRITE_TOOL[tool]
    shown = f"characters {offset}-{end} of {total} of the {_WHAT[tool]}"
    if more:
        sentence = (
            f"Partial read: {shown}. Pass offset={end} for the next part. This is not the whole "
            f"body, so do not write it back with {write}."
        )
    else:
        sentence = (
            f"Partial read: {shown}, the last part. This is not the whole body, so do not write "
            f"it back with {write}."
        )
    if comments_left_out:
        sentence += (
            f" {comments_left_out} older comment(s) are left out; they are in the Genos app."
        )
    cuts.next_call = sentence

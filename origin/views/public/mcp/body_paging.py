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

import hashlib
import logging
from typing import Any, Callable

from django.core.cache import cache

from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.note_targets import PERSONAL_FAMILY
from origin.views.public.mcp.result_limits import (
    BODY_FIELDS,
    MCP_RESULT_MAX_CHARS,
    Cuts,
    _env_int,
)

log = logging.getLogger(__name__)

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
    bad = ToolError(f"`{name}` must be a whole number (got {repr(value)[:40]}).")
    if isinstance(value, bool):
        raise bad
    if isinstance(value, float):
        # JSON allows 5.7, Infinity and NaN; the schema says integer.
        if not value.is_integer():
            raise bad
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise bad from None
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
            f"`offset` is past the end: the {_WHAT[tool]} has {total} characters. "
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

    # The part is sized in characters but the answer is measured as JSON text, where a newline, a
    # quote or a backslash costs two: dense markdown renders far larger than it counts. Shrink the
    # part until the answer, with the fields a partial read adds, fits, BEFORE `next_offset` is
    # set: a part the generic guard cut afterwards would leave a gap between two parts.
    target = cap - NOTICE_ROOM
    for _ in range(12):
        end = cut_point(body, offset, window)
        payload[field] = body[offset:end]
        probe = payload
        if offset or end < total:
            probe = {**payload, "body_total_chars": total, "next_offset": end}
        size = len(render(probe))
        if size <= target or window <= 200:
            break
        window = max(200, min(window - 1, int(window * target / size * 0.97)))
    payload[field] = body
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


# --------------------------------------------------------------------------
# a partial read is not written back
# --------------------------------------------------------------------------

#: How long "this caller has read only part of that body" is remembered, in seconds.
PARTIAL_READ_TTL = 3_600
#: The write tools, and the argument of each that REPLACES the whole body.
_REPLACES_BODY = {"update_note": "content_text", "update_task": "content_text"}
_READ_OF = {"update_note": "get_note", "update_task": "get_task"}
_WRITE_ALTERNATIVE = {
    "update_task": (
        "or change one part of it with `body_append`, `body_prepend` or `body_replace`, which do "
        "not need the rest"
    ),
    "update_note": (
        "or change just one block of it with `edit_note_block`: find the block with `get_note` and "
        "`blocks_matching` (a phrase it contains), which needs no part of the rest"
    ),
}


def _ident(tool: str, args: dict[str, Any]) -> str | None:
    if tool in ("get_task", "update_task"):
        return f"task:{args.get('task_id')}"
    if tool in ("get_note", "update_note"):
        # `update_note` takes `team` and `shared` for a note of the personal family (the same table
        # and ids as `personal`), and `get_note` only `personal`: one family, or a part read as
        # `personal` could be written back as `team`.
        note_type = str(args.get("note_type") or "").lower().strip()
        if note_type in PERSONAL_FAMILY:
            note_type = "personal"
        return f"note:{note_type}:{args.get('note_id')}"
    return None


def _marker(user_id: str, ident: str) -> str:
    return "mcp:partial-body:" + hashlib.sha256(f"{user_id}|{ident}".encode()).hexdigest()[:32]


def is_partial(tool: str, payload: dict[str, Any]) -> bool:
    """Is this answer less than the whole body? A part (it says `body_total_chars`), or a body the
    generic guard cut (the notice lists the field)."""
    if "body_total_chars" in payload:
        return True
    notice = payload.get("truncated")
    if isinstance(notice, dict) and "original_chars" in notice:
        fields = BODY_FIELDS.get(tool, ())
        return any(str(f).split(" ")[0] in fields for f in notice.get("fields") or ())
    return False


def remember_read(tool: str, args: dict[str, Any], payload: dict[str, Any], user_id: str) -> None:
    """After a read of a note or task: remember, for this caller and this item, whether what they
    were shown was partial. A complete read forgets it. A cache that fails changes nothing (the
    refusal below is then simply not made, as on a deployment without the switch)."""
    ident = _ident(tool, args)
    if ident is None:
        return
    try:
        if is_partial(tool, payload):
            cache.set(_marker(user_id, ident), 1, PARTIAL_READ_TTL)
        else:
            cache.delete(_marker(user_id, ident))
    except Exception:  # noqa: BLE001 - a marker never fails a read
        log.warning("MCP partial-read marker not stored for %s", tool, exc_info=True)


def refusal_for_write(tool: str, args: dict[str, Any], user_id: str) -> str | None:
    """The sentence that refuses a write which would REPLACE a body this caller has only partly
    read (`update_note` or `update_task` with `content_text`), else None. Nothing is changed."""
    field = _REPLACES_BODY.get(tool)
    if field is None or args.get(field) is None:
        return None
    ident = _ident(tool, args)
    try:
        partial = ident is not None and bool(cache.get(_marker(user_id, ident)))
    except Exception:  # noqa: BLE001 - fail open, as the rest of the layer does
        log.warning("MCP partial-read marker not read for %s", tool, exc_info=True)
        return None
    if not partial:
        return None
    what = _WHAT[_READ_OF[tool]]
    return (
        f"You have read only part of this {what} (`{_READ_OF[tool]}` answered in parts), and "
        f"`{field}` replaces the WHOLE body, so this write would delete the rest. Nothing was "
        f"changed. Read it completely first (a body of about 20,000 characters or less comes back "
        f"whole), {_WRITE_ALTERNATIVE[tool]}."
    )

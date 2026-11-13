"""How much CONTENT the in-app agent writes for a member (MIP-905, "Genos output size limitation").

A setting of its own, not part of effort: `CustomUser.preferred_content_length`, one of
`short`, `standard` (today's behaviour, the default) or `long`. It governs the bodies of the
tasks, milestones and notes Genos creates or edits and the plans it proposes (create_task,
update_task, create_note, update_note, create_task_plan, manage_milestone). Chat answers stay
concise at every level.

Two levers, both only while `SEARCH_ENGINE["AGENT_CONTENT_LENGTH"]` is on and only for a member
on `short` or `long`:

  * a compact, delimited guidance block appended at the very tail of the system instruction
    (after the date and language lines), so the shared rules text is not edited and the cached
    prefix is untouched;
  * an explicit per-call output cap on every provider (`GenerationParams.max_output_tokens`).

`standard` adds NOTHING and caps NOTHING: a member on Standard sends exactly what the flag-off
path sends, byte for byte. The flag off is the same: no query, no block, no cap.

Why a cap and not only words: a whole `create_task_plan` (up to 20 tasks and a milestone body) is
ONE model call's arguments, so it has to fit the per-call output budget; on Claude and OpenAI that
budget also pays for thinking, and Gemini Low is capped at 4096 while Medium/High send no cap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

LEVELS = ("short", "standard", "long")
DEFAULT_LEVEL = "standard"

#: Per-call output ceilings (tokens), every provider. Standard has none: it keeps whatever the
#: provider and effort profile send today.
SHORT_CAP = 4096
LONG_CAP = 16384

#: The tools whose arguments carry the content this setting is about. A run with none of them
#: available (a digest, the read-only agency level) gets no block and no cap.
CONTENT_WRITE_TOOLS = frozenset(
    {
        "create_task",
        "update_task",
        "create_note",
        "update_note",
        "create_task_plan",
        "manage_milestone",
    }
)

_SCOPE = (
    "This setting is about the CONTENT you write into the workspace for this member: the "
    "bodies of tasks, milestones and notes, and the tasks of a plan (create_task, update_task, "
    "create_note, update_note, create_task_plan, milestones). It does NOT change how you answer "
    "in chat: chat answers stay as concise as the rules above say, at every level."
)

_GUIDANCE = {
    "short": (
        "[CONTENT LENGTH: SHORT]\n"
        f"{_SCOPE}\n"
        "For that content write a BRIEF body: the point in one to three sentences, then at "
        "most three short bullets (acceptance criteria or next steps), only where they add "
        "something. About 60 words per task or milestone body and about 200 words for a note. "
        'This replaces the "~150 words" figure in the rules and in the plan tool\'s '
        "description. When editing part of an existing body, change only that part; do not "
        "trim the rest to fit this level. If the member asks for more or less detail in a "
        "request, follow the request. (Japanese: count characters, about 2.5 per word.)\n"
        "[END CONTENT LENGTH]"
    ),
    "long": (
        "[CONTENT LENGTH: LONG]\n"
        f"{_SCOPE}\n"
        "For that content write a FULL body. Per task or milestone body aim for about 400 "
        "words, with the house template's headings (Summary, Motivation, Approach or steps, "
        "Acceptance criteria) and a Risks / open questions section where there are any; a "
        "note is as long as its subject needs, with headings, lists and tables where they "
        'help, up to about 1,500 words. The "~150 words" figure in the rules and in the plan '
        "tool's description, and the preference for 3-5 bullets, do not apply to this content "
        "(they stay in force for chat answers). A whole create_task_plan must fit in ONE "
        "reply (about 16,000 tokens of output; Japanese takes two to three times the tokens "
        "of English): for a plan of more than 10 tasks, or a Japanese plan of more than 8, "
        "write shorter bodies or put the remaining tasks in a second plan. Still at most 20 "
        "tasks per plan. When editing part of an existing body, change only that part; do "
        "not pad the rest to fit this level. If the member asks for more or less detail in "
        "a request, follow the request. (Japanese: count characters, about 2.5 per word.)\n"
        "[END CONTENT LENGTH]"
    ),
}


def normalize_level(value: object) -> str:
    """`short` / `standard` / `long`; anything else (including "" and None) is `standard`."""
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in LEVELS else DEFAULT_LEVEL


# The eval harness decides a case's level from the case, not from the shared fixture member's
# row (parallel cases would race on it). Checked FIRST by `member_level`. A ContextVar, like
# the case locale: pool threads that run a case set it for that case only.
_level_override: ContextVar[str | None] = ContextVar("content_length_override", default=None)


@contextmanager
def override_level(level: str | None) -> Iterator[None]:
    """Inside the block, `member_level` answers `level` without reading the database."""
    token = _level_override.set(normalize_level(level) if level else None)
    try:
        yield
    finally:
        _level_override.reset(token)


def member_level(user_id: str | None) -> str:
    """The member's saved level. FAIL-OPEN: a missing/malformed id or an unavailable database
    is `standard`, never a failed ask (the same rule the date line holds itself to)."""
    forced = _level_override.get()
    if forced is not None:
        return forced
    try:
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        value = (
            CustomUser.objects.filter(pk=user_id)
            .values_list("preferred_content_length", flat=True)
            .first()
        )
    except Exception:  # noqa: BLE001 — see FAIL-OPEN above
        log.warning("content-length: preference lookup failed for %r; using standard", user_id)
        return DEFAULT_LEVEL
    return normalize_level(value)


def resolve_level(user_id: str | None, tool_names: Iterable[str]) -> str:
    """The level that applies to THIS run: the member's, but `standard` when the run cannot
    write content at all (none of `CONTENT_WRITE_TOOLS` among its tools). Does not read the
    flag: the caller does, and does not call this with the flag off."""
    if not CONTENT_WRITE_TOOLS.intersection(tool_names):
        return DEFAULT_LEVEL
    return member_level(user_id)


def guidance_block(level: str) -> str:
    """The block for `level`; "" for `standard` (and anything unknown)."""
    return _GUIDANCE.get(level, "")


def max_output_tokens(level: str) -> int | None:
    """The per-call output cap for `level`; None for `standard` (today's value, untouched)."""
    if level == "short":
        return SHORT_CAP
    if level == "long":
        return LONG_CAP
    return None


# --------------------------------------------------------------------------- #
# Which calls carry a body (AGENT_BODY_ON_USER_MODEL)                         #
# --------------------------------------------------------------------------- #

_BODY_ARGS = {
    "create_task": ("content_text",),
    "update_task": ("content_text", "body_append", "body_prepend", "body_replace"),
    "create_note": ("content_text",),
    "update_note": ("content_text",),
    "manage_milestone": ("description_markdown",),
}


def _present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_present(v) for v in value.values())
    return bool(value)


def carries_body(name: str, args: dict[str, Any] | None) -> bool:
    """True when this write call carries a body the model wrote: a create or update of a task,
    a note or a milestone with a non-empty body argument, or a plan with any task or milestone
    body. A status-only `update_task` is not one (nothing to write well), and a read tool never
    is."""
    args = args or {}
    if name == "create_task_plan":
        milestone = args.get("milestone")
        if isinstance(milestone, dict) and _present(milestone.get("description_markdown")):
            return True
        tasks = args.get("tasks")
        return isinstance(tasks, list) and any(
            isinstance(t, dict) and _present(t.get("content_markdown")) for t in tasks
        )
    keys = _BODY_ARGS.get(name)
    return bool(keys) and any(_present(args.get(k)) for k in keys)

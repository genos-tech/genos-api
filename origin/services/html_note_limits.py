"""HTML notes by plan: who may save a page and how much (MIP-2216, after MIP-2214).

ONE table, `HTML_NOTE_LIMITS`, holds every number. Tune a limit by editing a row here (Ken set these
on 2026-11-23); nothing else in the code carries a size, a count or a rate. The shape follows
`SKILLS_MAX_BY_TIER` in `services/skills/service.py`: keyed by the member's EFFECTIVE tier, and kept
here until the plan table (`TIER_QUOTAS`, the billing page) grows keys for it.

- Free has no row (`None`): HTML notes are a Core feature. A Free member's save stores nothing and is
  answered with a suggestion (`HtmlNotesNotOnPlan`), never an error status.
- Core: 512 KB per page, 100 HTML notes per member, 30 saves per hour.
- Pro, Max and Enterprise: 1 MB per page, 500 notes, 60 saves per hour.

READING IS NEVER GATED. A member who downgrades keeps their pages and can open them (the read
endpoint and `get_note` do not call this module) and nothing is deleted; only NEW saves are held to
the new plan. A count over the new limit just means no new note until some are deleted.

Not on the billed-eval path: this module imports no retrieval code, and the plan lookup is imported
inside the function, as the skills service does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster

logger = logging.getLogger(__name__)

BODY_FORMAT_HTML = "html"
#: The cheapest plan that has HTML notes, said in every reply to a member who does not.
REQUIRED_TIER = "core"
#: Where a member changes their plan, as the MCP plan gate words it.
PLAN_PLACE = "Settings → Plan & Usage"


@dataclass(frozen=True)
class HtmlNoteLimits:
    max_bytes: int  # one page, UTF-8 bytes
    max_notes: int  # HTML notes one member owns
    saves_per_hour: int  # saves across all of the member's HTML notes


_CORE = HtmlNoteLimits(max_bytes=512 * 1024, max_notes=100, saves_per_hour=30)
_PRO_AND_UP = HtmlNoteLimits(max_bytes=1024 * 1024, max_notes=500, saves_per_hour=60)

#: THE TABLE. `None` = not available on that plan.
HTML_NOTE_LIMITS: dict[str, HtmlNoteLimits | None] = {
    "free": None,
    "core": _CORE,
    "pro": _PRO_AND_UP,
    "max": _PRO_AND_UP,
    "enterprise": _PRO_AND_UP,
}

#: The largest page any plan may save: the ceiling for code that has no member to ask (a scan of
#: stored text, a timing test). Derived, so it can never disagree with the table.
MAX_PAGE_BYTES = max(row.max_bytes for row in HTML_NOTE_LIMITS.values() if row is not None)


class HtmlNotesNotOnPlan(Exception):
    """The member's plan has no HTML notes. `reply()` is the suggestion to give them; nothing was
    stored. Not a failure: callers answer with the reply instead of an error."""

    def __init__(self, tier: str) -> None:
        self.tier = tier
        super().__init__(not_on_plan_message(tier))

    def reply(self) -> dict[str, Any]:
        message = str(self)
        return {
            "saved": False,
            "created": False,
            "plan_required": REQUIRED_TIER,
            "your_plan": self.tier,
            "message": message,
            "next_step": (
                "Nothing was saved. Tell the user HTML notes are a Core plan feature and that they "
                f"can upgrade in {PLAN_PLACE}. Meanwhile, offer the same content as an ordinary "
                "note with `create_note` (headings, lists and tables), which every plan has."
            ),
            "__summary__": "Not saved: HTML notes are a Core plan feature",
        }


class HtmlNoteLimitReached(Exception):
    """A number in the table was hit. The message names the limit, the numbers and the fix."""


def not_on_plan_message(tier: str) -> str:
    return (
        f"HTML notes are a Core plan feature, and the {tier.title()} plan does not include them, "
        "so this page was not saved. Existing HTML notes stay readable. To save pages like this, "
        f"upgrade to Core or higher in {PLAN_PLACE}, or ask for the same content as an ordinary "
        "note, which every plan has."
    )


def tier_of(user_id: Any) -> str:
    """The member's effective tier. A failure while reading the plan reads as Core (the cheapest
    plan with the feature): the member is never blocked by our failure, and the strictest numbers
    that still work apply."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    try:
        return quota.get_effective_tier(str(user_id))
    except Exception:  # noqa: BLE001
        logger.exception("html_note_limits: tier lookup failed for user=%s", user_id)
        return REQUIRED_TIER


def limits_for_tier(tier: str) -> HtmlNoteLimits | None:
    """The row for `tier`; an unknown name reads as Free (no HTML notes)."""
    return HTML_NOTE_LIMITS.get(tier)


def limits_for(user_id: Any) -> HtmlNoteLimits:
    """The member's limits, or `HtmlNotesNotOnPlan` when their plan has none."""
    tier = tier_of(user_id)
    row = limits_for_tier(tier)
    if row is None:
        raise HtmlNotesNotOnPlan(tier)
    return row


def count_owned(user_id: Any) -> int:
    """HTML notes this member owns, across personal, task and chat notes."""
    return sum(
        model.objects.filter(owner_id=user_id, body_format=BODY_FORMAT_HTML).count()
        for model in (PersonalNoteMaster, TaskNoteMaster, ChatNoteMaster)
    )


def check_room_for_new_note(user_id: Any, limits: HtmlNoteLimits) -> None:
    """Refuse a NEW HTML note when the member already owns the plan's count. A replace of an
    existing page adds no note and does not call this."""
    owned = count_owned(user_id)
    if owned >= limits.max_notes:
        raise HtmlNoteLimitReached(
            f"You own {owned} HTML notes and your plan keeps up to {limits.max_notes}. Nothing was "
            "saved. Delete HTML notes you no longer need, or replace the page of an existing HTML "
            "note (call `save_html_note` with its `note_id`) instead of creating another. A higher "
            f"plan keeps more: see {PLAN_PLACE}."
        )

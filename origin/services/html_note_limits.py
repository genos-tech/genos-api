"""HTML notes by plan: who may save a page and how much (MIP-2216, after MIP-2214).

ONE table, `HTML_NOTE_LIMITS`, holds every number. Tune a limit by editing a row here (Ken set these
on 2026-11-23 and 2026-11-27); nothing else in the code carries a size, a quota or a rate. The shape follows
`SKILLS_MAX_BY_TIER` in `services/skills/service.py`: keyed by the member's EFFECTIVE tier, and kept
here until the plan table (`TIER_QUOTAS`, the billing page) grows keys for it.

- Free has no row (`None`): HTML notes are a Core feature. A Free member's save stores nothing and is
  answered with a suggestion (`HtmlNotesNotOnPlan`), never an error status.
- Core: 512 KB per page, 200 MB of storage per member, 30 saves per hour.
- Pro, Max and Enterprise: 1 MB per page, 1 GB of storage, 60 saves per hour.

THE NUMBER OF HTML NOTES IS NOT LIMITED (MIP-2453). What costs is storage, so the quota counts the
bytes of every kept revision (`html_notes.KEEP_REVISIONS` per note) of the HTML notes a member OWNS.
A save that adds to it past the quota is refused; one that adds nothing (a page no bigger than the
revision it pushes out) is always taken, even by a member already over.

READING IS NEVER GATED. A member who downgrades keeps their pages and can open them (the read
endpoint and `get_note` do not call this module) and nothing is deleted; only NEW saves are held to
the new plan. Usage over the new quota just means no save that adds to it until some are deleted.

Not on the billed-eval path: this module imports no retrieval code, and the plan lookup is imported
inside the function, as the skills service does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.db.models import Subquery, Sum, Value
from django.db.models.functions import Coalesce

from origin.models.common.user_models import CustomUser
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
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
    max_storage_bytes: int  # kept revisions of the HTML notes one member owns, together
    saves_per_hour: int  # saves across all of the member's HTML notes


_CORE = HtmlNoteLimits(max_bytes=512 * 1024, max_storage_bytes=200 * 1024 * 1024, saves_per_hour=30)
_PRO_AND_UP = HtmlNoteLimits(
    max_bytes=1024 * 1024, max_storage_bytes=1024 * 1024 * 1024, saves_per_hour=60
)

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
_MAX_STORAGE_BYTES = max(
    row.max_storage_bytes for row in HTML_NOTE_LIMITS.values() if row is not None
)


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


def storage_used(user_id: Any) -> int:
    """Bytes of every kept revision of the HTML notes this member owns, across personal, task and
    chat notes. One query: a sum per family, each reading the revision index for the owned ids."""

    def family(code: int, model) -> Coalesce:
        owned = model.objects.filter(owner_id=user_id, body_format=BODY_FORMAT_HTML)
        total = (
            NoteHtmlRevision.objects.filter(note_type=code, note_id__in=owned.values("note_id"))
            .values("note_type")
            .annotate(total=Sum("size"))
            .values("total")
        )
        return Coalesce(Subquery(total), Value(0))

    # The codes are `NoteHtmlRevision.note_type`: 1 personal, 2 task, 3 chat.
    used = (
        CustomUser.objects.filter(id=user_id)
        .annotate(
            used=family(1, PersonalNoteMaster)
            + family(2, TaskNoteMaster)
            + family(3, ChatNoteMaster)
        )
        .values_list("used", flat=True)
        .first()
    )
    return used or 0


def size_text(size: int) -> str:
    """A number of bytes as a person reads it: "850 KB", "199.5 MB", "1 GB"."""
    for unit, name, places in ((1024**3, "GB", 2), (1024**2, "MB", 1)):
        if size >= unit:
            return f"{size / unit:.{places}f}".rstrip("0").rstrip(".") + f" {name}"
    return f"{-(-size // 1024)} KB"


def check_storage(
    user_id: Any,
    limits: HtmlNoteLimits,
    *,
    new_bytes: int,
    freed_bytes: int = 0,
    owner_id: Any = None,
    replacing: bool = False,
) -> None:
    """Refuse a save that would take the note OWNER's HTML storage past their plan's quota.

    `limits` is the saver's row. `owner_id` is the note's owner when a replace edits a note, since
    the new revision counts against the owner; None for a new note (the saver will own it).
    `freed_bytes` is the size of the revisions this save drops. A save that does not add to the
    usage is never refused. Someone else's numbers are not told to the saver.
    """
    added = new_bytes - freed_bytes
    if added <= 0:
        return
    own = owner_id is None or str(owner_id) == str(user_id)
    if not own:
        user_id = owner_id
        # An owner whose plan no longer has HTML notes is held to the smallest quota there is.
        limits = limits_for_tier(tier_of(owner_id)) or HTML_NOTE_LIMITS[REQUIRED_TIER]
    used = storage_used(user_id)
    if used + added <= limits.max_storage_bytes:
        return
    if not own:
        raise HtmlNoteLimitReached(
            "This note's owner has no HTML storage left for a bigger version, so nothing was "
            "saved. Ask them to delete HTML notes they no longer need, or save the page as a new "
            "HTML note of your own."
        )
    if not replacing:
        instead = (
            ", or replace the page of an existing HTML note (call `save_html_note` with its "
            "`note_id`) instead of creating another"
        )
    elif limits.max_storage_bytes - used + freed_bytes > 0:
        instead = ", or make this page smaller"
    else:
        instead = ""  # no size of page would be taken
    upgrade = (
        f" A higher plan has more storage: see {PLAN_PLACE}."
        if limits.max_storage_bytes < _MAX_STORAGE_BYTES
        else ""
    )
    # The numbers come first: a failed dashboard run keeps only the start of this sentence.
    raise HtmlNoteLimitReached(
        f"Your HTML pages use {size_text(used)} of your {size_text(limits.max_storage_bytes)} "
        "storage, so this page was not saved. Delete HTML notes you no longer need (that frees all "
        f"their kept versions at once){instead}.{upgrade}"
    )

"""What Genos remembers about a member, to see and to delete (MIP-1088).

The secretary keeps three kinds of things about a member, all private to them, all made from
what they do and never from anything they typed to Genos:

  * the LEDGER (`MemberEvent`, kept 180 days): which items their AI apps opened or changed
    over MCP, and their own changes nothing else saves (references and times, never text);
  * the SUMMARIES (`MemberItemDay`, kept 365 days): how busy each task was on each local day,
    by kind (counts, never text, never time spent);
  * what they CORRECTED (`MemberImportanceMark`, `MemberImportanceTuning`): the tasks they
    marked and how much each signal counts for them.

This module is the one place that says what is stored (`stored`), what Genos has noticed
(`overview`, with `importance.describe` for the items) and how to delete it (`forget`). The
REST page (`views/common/memory_views.py`) and the two MCP tools (`get_my_memory`,
`forget_my_memory`) are thin wrappers over it, so what the page shows is what Claude is told
and what a delete removes is the same in both.

**Account-wide counts, account-wide deletes.** The recording switch is the member's, not a
team's, so `stored` counts and `forget` removes the member's rows in EVERY team: the number the
page shows is the number a delete takes away. Marks and tuning are per team and are shown for
the team the page is in.

**Owner-only.** Every function takes the caller's id (or `ToolContext`) and filters on it; none
takes another user.

**A delete is a real purge, and it stays one.** The summaries are derived from task history, so
deleting them while recording is on would be undone by the next briefing (it refills up to a
week from the sources). `forget` therefore moves the fill watermark to today (the member's
day): nothing from before the delete is rebuilt, not even the rest of the day it happened on;
the next day is recorded as usual. Turning recording OFF is not a delete tool here: it is the existing switch
(`member_events_enabled`), and it purges all three by itself.

It takes the same user-row lock as that switch, so a recorder that is writing right now either
finishes first (and is purged) or waits and writes a fresh row afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Min, Q

from origin.models.common.item_activity_models import MemberItemDay
from origin.models.common.member_event_models import MCP_KINDS, MemberEvent
from origin.models.common.member_importance_models import (
    MemberImportanceMark,
    MemberImportanceTuning,
)
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import importance, item_activity, member_events

LANGUAGE = "en"

# What `forget` can delete, in the words both doors use.
WHAT = ("importance", "summaries", "events", "everything")


class UnknownWhat(ValueError):
    """`what` is not one of `WHAT`."""


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def stored(user_id: Any) -> dict[str, Any]:
    """What is in the tables for this member, in every team: counts and date ranges, never a
    row's content. Rows are counted as they are, including those of a task the member can no
    longer open: this is what a delete removes."""
    me = str(user_id)
    ledger = MemberEvent.objects.filter(user_id=me).aggregate(
        entries=Count("pk"),
        ai_app=Count("pk", filter=Q(kind__in=MCP_KINDS)),
        first=Min("ts"),
        last=Max("ts"),
    )
    days = MemberItemDay.objects.filter(user_id=me).aggregate(
        rows=Count("pk"),
        items=Count("task_id", distinct=True),
        first=Min("day"),
        last=Max("day"),
    )
    tuned = sum(
        len(importance.clean_levels(weights))
        for weights in MemberImportanceTuning.objects.filter(user_id=me).values_list(
            "weights", flat=True
        )
    )
    return {
        "ledger": {
            "entries": ledger["entries"],
            "ai_app": ledger["ai_app"],
            "own_changes": ledger["entries"] - ledger["ai_app"],
            "from": _iso(ledger["first"]),
            "to": _iso(ledger["last"]),
            "kept_days": member_events.RETENTION.days,
        },
        "summaries": {
            "rows": days["rows"],
            "items": days["items"],
            "from": _iso(days["first"]),
            "to": _iso(days["last"]),
            "kept_days": item_activity.RETENTION.days,
        },
        "importance": {
            "marks": MemberImportanceMark.objects.filter(user_id=me).count(),
            "tuned_signals": tuned,
        },
    }


def catalog() -> dict[str, Any]:
    """The words a page needs for the signals, levels and marks: a `key` and English text. The
    page translates by key; nothing here is a sentence built from data."""
    return {
        "levels": [
            {"key": key, "name": name, "means": means}
            for key, (name, means) in importance.LEVEL_WORDS.items()
        ],
        "marks": [
            {"key": key, "name": name, "means": means}
            for key, (name, means) in importance.MARK_WORDS.items()
        ],
    }


def tuning(prefs: importance.Prefs) -> list[dict[str, str]]:
    """Every signal with its level (the default when untouched) and its words."""
    return [
        {
            "signal": name,
            "level": prefs.levels.get(name, importance.DEFAULT_LEVEL),
            "name": importance.SIGNAL_NAMES[name],
            "means": importance.SIGNAL_WORDS[name],
        }
        for name in importance.SIGNALS
    ]


def marks(ctx: ToolContext, prefs: importance.Prefs) -> list[dict[str, Any]]:
    """This team's marked tasks, only those the member can open now (the same rule as every
    read), the title read live. Raw title: a page escapes it; the MCP door fences it."""
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

    readable = LabelAccess(ctx).tasks(set(prefs.marks))
    return [
        {
            "task_id": task.task_id,
            "display_id": task.display_id,
            "title": task.title or "",
            "mark": prefs.marks[task.task_id],
        }
        for task in sorted(readable.values(), key=lambda t: t.task_id)
    ]


def overview(ctx: ToolContext) -> dict[str, Any]:
    """The whole settings page in one cheap read (no ranking): the switch, what is stored, this
    team's tuning and marks, and the words. `ctx.team_id` must already be a team the caller is
    in (the view checks it)."""
    prefs = importance.load(ctx)
    return {
        "language": LANGUAGE,
        "recording": {"enabled": importance.enabled(ctx.user_id)},
        "stored": stored(ctx.user_id),
        "tuning": tuning(prefs),
        "marks": marks(ctx, prefs),
        "marks_limit": importance.MAX_MARKS,
        "catalog": catalog(),
    }


def forget(user_id: Any, what: str, now: datetime | None = None) -> dict[str, Any]:
    """Delete what Genos remembers about this member, in every team: `importance` (marks and
    tuning), `summaries` (the per-day table and its watermark), `events` (the ledger) or
    `everything`. Returns `{"what", "deleted", "stored"}`: how much of each went, and what is
    left. The recording switch is not touched. Raises `UnknownWhat` for anything else."""
    if what not in WHAT:
        raise UnknownWhat(what)
    me = str(user_id)
    gone: dict[str, int] = {}
    with transaction.atomic():
        # The row lock the switch and every writer use: see the module docstring.
        user = CustomUser.objects.select_for_update().filter(pk=me).first()
        before = stored(me)
        if what in ("events", "everything"):
            MemberEvent.objects.filter(user_id=me).delete()
            gone["events"] = before["ledger"]["entries"]
        if what in ("summaries", "everything"):
            item_activity.purge(me)
            if user is not None and user.member_events_enabled:
                # Derived data refills from task history: record from tomorrow on. The next
                # fill must not build the day of the delete either (it would bring back
                # this morning), or the delete would be undone by the next briefing.
                item_activity.start_fresh(me, now, through_today=True)
            gone["summaries"] = before["summaries"]["rows"]
        if what in ("importance", "everything"):
            importance.purge(me)
            gone["marks"] = before["importance"]["marks"]
            gone["tuned_signals"] = before["importance"]["tuned_signals"]
    return {"what": what, "deleted": gone, "stored": stored(me)}

"""The signed-in member's own AI credit usage (MIP-1971).

Reads the credit ledger (`AiCreditEntry`, charges only) and the request rollup (`AiRequestCost`,
for the surface of each charge). No money logic lives here: a charge is what the ledger already
posted. Everything is scoped to ONE user id, taken from the request by the view, never from input.

The job names are plain-language KEYS, translated by the client. A surface this file does not know
maps to `other`, never to an error.

MIP-2024: every surface in `credit_policy.yaml` `billable_surfaces` has its own job key, so `other`
is only a source added after this file (or a row from before one existed). The MCP search is
metered under `mcp_search` (MIP-1965); the first version of this file knew it only as `mcp`, which
is the plain, never-charged run, so every charged MCP search landed in "Other AI use".

Recent charges page by a keyset cursor (the ledger row id, newest first): `limit` rows a page,
`next_cursor` is null on the last page. The row carries no connected-app name: the ledger and the
request rollup do not store it, so a charge says "connected app", not which one.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from django.utils import timezone

log = logging.getLogger(__name__)

RECENT_DEFAULT_LIMIT = 5
RECENT_MAX_LIMIT = 25
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# surface (AiRequestCost.surface) -> job key. Anything else is JOB_OTHER.
JOB_ASK = "ask"
JOB_SUMMARY = "summary"
JOB_SCHEDULE = "schedule"
JOB_CONNECTED_APP = "connected_app"
JOB_OTHER = "other"

_SURFACE_JOB = {
    "ask": JOB_ASK,
    "thread_summary": JOB_SUMMARY,
    "note_summary": JOB_SUMMARY,
    "schedule": JOB_SCHEDULE,
    "mcp_search": JOB_CONNECTED_APP,
    # The plain MCP run (no model call) is never charged; kept so a stray row is still named.
    "mcp": JOB_CONNECTED_APP,
}

# Display order of the groups; `other` is last.
JOB_ORDER = (JOB_ASK, JOB_CONNECTED_APP, JOB_SCHEDULE, JOB_SUMMARY, JOB_OTHER)


def job_for_surface(surface: str | None) -> str:
    return _SURFACE_JOB.get(surface or "", JOB_OTHER)


def _credits(milli: int) -> float:
    """Milli-credits to credits, 2 decimals (a request can cost 0.11)."""
    return round(milli / 1000, 2)


def _surfaces(request_ids: set) -> dict[str, str]:
    from origin.search_engine.models import AiRequestCost  # noqa: PLC0415

    if not request_ids:
        return {}
    return {
        str(rid): surface
        for rid, surface in AiRequestCost.objects.filter(request_id__in=request_ids).values_list(
            "request_id", "surface"
        )
    }


def _schedule_names(user, request_ids: list[str]) -> dict[str, str]:
    """`request_id -> schedule name` for scheduled-report charges, from the member's OWN Inbox
    items (the item carries the paying request in its `ai` block and the schedule id)."""
    from origin.models.common.inbox_models import InboxItems  # noqa: PLC0415
    from origin.models.common.schedule_models import MemberSchedule  # noqa: PLC0415

    if not request_ids:
        return {}
    by_request: dict[str, str] = {}
    try:
        for optionals in InboxItems.objects.filter(
            receiver=user, item_optionals__ai__request_id__in=request_ids
        ).values_list("item_optionals", flat=True):
            if isinstance(optionals, dict):
                rid = str((optionals.get("ai") or {}).get("request_id") or "")
                sid = str(optionals.get("schedule_id") or "")
                if rid and _UUID.match(sid):
                    by_request[rid] = sid
        names = {
            str(s.id): s.name
            for s in MemberSchedule.objects.filter(
                user_id=user.pk, id__in=list(set(by_request.values()))
            )
        }
    except Exception:  # noqa: BLE001 - a missing name never fails the page
        log.debug("schedule names not read", exc_info=True)
        return {}
    return {rid: names[sid] for rid, sid in by_request.items() if sid in names}


def clamp_limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return RECENT_DEFAULT_LIMIT
    return min(max(value, 1), RECENT_MAX_LIMIT)


def _reversed_milli(user_id: str, entry_ids: set[int]) -> dict[int, int]:
    """A refund is a separate `reversal` row pointing at the charge (`ref_id`): net it, so the
    totals here agree with the balance, which already includes it."""
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    out: dict[int, int] = defaultdict(int)
    for ref_id, milli in AiCreditEntry.objects.filter(
        user_id=user_id, entry_type=AiCreditEntry.ENTRY_REVERSAL, ref_id__in=entry_ids
    ).values_list("ref_id", "credits_milli"):
        out[ref_id] += int(milli)
    return out


def recent_for(
    user, *, limit: int = RECENT_DEFAULT_LIMIT, cursor: int | None = None
) -> tuple[list[dict[str, Any]], int | None]:
    """One page of the caller's charges, newest first: `(rows, next_cursor)`.

    The cursor is the id of the last row of the previous page (ledger ids only grow, so id order
    is time order and a new charge never shifts a page). `next_cursor` is None on the last page.
    One extra row is read to know that, without a COUNT over the ledger. Only `user`'s own rows
    are ever read: the user comes from the request, the cursor is just a position in that set.
    """
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    user_id = str(user.pk)
    limit = clamp_limit(limit)
    qs = AiCreditEntry.objects.filter(user_id=user_id, entry_type=AiCreditEntry.ENTRY_CHARGE)
    if cursor is not None:
        qs = qs.filter(id__lt=cursor)
    rows = list(
        qs.order_by("-id").values_list("id", "request_id", "credits_milli", "created_at")[
            : limit + 1
        ]
    )
    more = len(rows) > limit
    rows = rows[:limit]

    reversed_milli = _reversed_milli(user_id, {r[0] for r in rows})
    surfaces = _surfaces({r[1] for r in rows if r[1] is not None})
    schedule_rids = [
        str(r[1]) for r in rows if job_for_surface(surfaces.get(str(r[1]))) == JOB_SCHEDULE
    ]
    names = _schedule_names(user, schedule_rids)

    charges = []
    for entry_id, rid, milli, created_at in rows:
        surface = surfaces.get(str(rid), "")
        job = job_for_surface(surface)
        charges.append(
            {
                "id": entry_id,
                "at": created_at.isoformat(),
                "job": job,
                "credits": _credits(max(-int(milli) - reversed_milli.get(entry_id, 0), 0)),
                "reversed": reversed_milli.get(entry_id, 0) > 0,
                "detail": names.get(str(rid), "") if job == JOB_SCHEDULE else "",
                # The raw source name, only for a charge no job names, so the row can say what it was.
                "source": surface if job == JOB_OTHER else "",
            }
        )
    return charges, (rows[-1][0] if more and rows else None)


def usage_for(
    user, *, tier: str, period: str | None = None, limit: int = RECENT_DEFAULT_LIMIT
) -> dict[str, Any]:
    """The payload of GET /api/v2/agent/credits/usage/ for `user` (the caller)."""
    from origin.search_engine import credit_ledger  # noqa: PLC0415
    from origin.search_engine.agent_views import _credits_block  # noqa: PLC0415
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    user_id = str(user.pk)
    period = period or credit_ledger.period_for()
    block = _credits_block(user_id, tier)  # None unless credits are authoritative

    base = AiCreditEntry.objects.filter(user_id=user_id, entry_type=AiCreditEntry.ENTRY_CHARGE)
    month_rows = list(base.filter(period=period).values_list("id", "request_id", "credits_milli"))
    reversed_milli = _reversed_milli(user_id, {r[0] for r in month_rows})
    surfaces = _surfaces({r for _, r, _ in month_rows if r is not None})

    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for entry_id, rid, milli in month_rows:
        net = -int(milli) - reversed_milli.get(entry_id, 0)  # a reversal is positive
        if net <= 0:
            continue  # fully refunded: not a use of credits any more
        job = job_for_surface(surfaces.get(str(rid)))
        counts[job] += 1
        totals[job] += net

    charges, next_cursor = recent_for(user, limit=limit)

    return {
        "credits": block,  # same shape as the `credits` block of /agent/features/
        "tier": tier,
        "period": period,
        "period_end_iso": block["period_end_iso"] if block else None,
        "by_job": [
            {"job": job, "count": counts[job], "credits": _credits(totals[job])}
            for job in JOB_ORDER
            if counts[job]
        ],
        "month_total_credits": _credits(sum(totals.values())),
        "recent": charges,
        "next_cursor": next_cursor,
        "generated_at": timezone.now().isoformat(),
    }

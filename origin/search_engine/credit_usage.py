"""The signed-in member's own AI credit usage (MIP-1971).

Reads the credit ledger (`AiCreditEntry`, charges only) and the request rollup (`AiRequestCost`,
for the surface of each charge). No money logic lives here: a charge is what the ledger already
posted. Everything is scoped to ONE user id, taken from the request by the view, never from input.

The job names are plain-language KEYS, translated by the client. A surface this file does not know
(a new source such as the MCP search of MIP-1965) maps to `other`, never to an error.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from django.utils import timezone

log = logging.getLogger(__name__)

RECENT_LIMIT = 50

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
                if rid and sid:
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


def usage_for(user, *, tier: str, period: str | None = None) -> dict[str, Any]:
    """The payload of GET /api/v2/agent/credits/usage/ for `user` (the caller)."""
    from origin.search_engine import credit_ledger  # noqa: PLC0415
    from origin.search_engine.agent_views import _credits_block  # noqa: PLC0415
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    user_id = str(user.pk)
    period = period or credit_ledger.period_for()
    block = _credits_block(user_id, tier)  # None unless credits are authoritative

    base = AiCreditEntry.objects.filter(user_id=user_id, entry_type=AiCreditEntry.ENTRY_CHARGE)
    month_rows = list(base.filter(period=period).values_list("request_id", "credits_milli"))
    recent = list(
        base.order_by("-created_at", "-id").values_list(
            "id", "request_id", "credits_milli", "created_at"
        )[:RECENT_LIMIT]
    )

    surfaces = _surfaces(
        {r for r, _ in month_rows if r is not None} | {r[1] for r in recent if r[1] is not None}
    )

    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for rid, milli in month_rows:
        job = job_for_surface(surfaces.get(str(rid)))
        counts[job] += 1
        totals[job] += -int(milli)

    schedule_rids = [
        str(r[1]) for r in recent if job_for_surface(surfaces.get(str(r[1]))) == JOB_SCHEDULE
    ]
    names = _schedule_names(user, schedule_rids)

    charges = []
    for entry_id, rid, milli, created_at in recent:
        job = job_for_surface(surfaces.get(str(rid)))
        charges.append(
            {
                "id": entry_id,
                "at": created_at.isoformat(),
                "job": job,
                "credits": _credits(-int(milli)),
                "detail": names.get(str(rid), "") if job == JOB_SCHEDULE else "",
            }
        )

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
        "generated_at": timezone.now().isoformat(),
    }

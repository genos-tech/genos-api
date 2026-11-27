"""The monthly AI spend limits, as a SOFT stop (MIP-2363, decided by the maintainer 2026-11-27).

Two operator settings, both 0 (off) by default:

* `AI_MONTHLY_BUDGET_JPY`: the whole product's AI spend in one month, in yen.
* `AI_TEAM_MONTHLY_CEILING_USD`: one team's AI spend in one month, in US dollars.

Once a month's spend reaches either of them, AI features step back to their plain versions until the
month ends (UTC), the same way as for a member with no AI credits left. This module only answers
"is a limit reached?". The three places that already decide that for credits ask it first and take
their existing zero-credit way out, so there is no second skip path:

* `agent_views._credit_gate` (ask, note summary, thread summary): a 429 that says so;
* `report_ai.llm.credit_block` (scheduled reports, skill runs, dashboards): filed plain;
* `mcp_search_credits.plain_reason` (MCP `search_workspace`): a plain search.

It is Genos's own money limit, not a customer's credit, so it holds while credits are only shadowed
and for unlimited plans too. Not stopped: what members are never charged for and what keeps data
fresh (index and query embeddings, Spotlight search, the judge sampler, evals), the approval or
decline of an ask that is already paused, and a run already under way.

Spend is read from the cost ledger (`AiSpendEvent`), so with `AI_COST_METER` off nothing is counted.
The totals are cached for 5 minutes. Fails OPEN: a limit that cannot be read stops nothing. The
settings must have the same values on every service that runs AI (web and `schedule-tick`).
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

log = logging.getLogger(__name__)

# Money-free on purpose: the member never chose a budget and cannot act on a figure. No "buy
# credits" either: buying would not lift this.
MONTHLY_LIMIT_MESSAGE = (
    "Genos AI has reached its monthly limit, so it can't do this right now. It comes back at the "
    "start of next month. Search and everything else still work."
)

_CACHE_SECONDS = 300
_LOG_EVERY_SECONDS = 3600


def canonical_team_id(team_id) -> str:
    """The one spelling of a team id on the ledger (MIP-2450). Views take it from the request body
    and the membership check accepts a UUID in any spelling, so the ledger is written and read
    through this. Anything that is not a UUID is kept as it is; no id is ""."""
    raw = str(team_id or "")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return raw


def _month_events():
    from origin.search_engine.models import AiSpendEvent  # noqa: PLC0415

    since = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return AiSpendEvent.objects.filter(created_at__gte=since)


def _cached_sum(cache_key: str, queryset, field: str) -> int:
    cached = cache.get(cache_key)
    if cached is not None:
        return int(cached)
    total = int(queryset.aggregate(s=Sum(field))["s"] or 0)
    cache.set(cache_key, total, _CACHE_SECONDS)
    return total


def month_spend_usd_micro(*, user_id: str = "", team_id: str = "") -> int:
    """This UTC month's ledger spend for one user OR one team, in micro-USD. Cached for 5
    minutes: a ceiling drifts by at most a few asks, which is fine for a monthly number. Raises on
    cache/DB trouble; callers fail open.

    USD because the ceilings it feeds are COST ceilings and cost arrives in dollars. A ceiling
    denominated in yen would tighten or loosen itself every time the exchange rate moved.
    """
    team_id = canonical_team_id(team_id)
    key_id = f"u:{user_id}" if user_id else f"t:{team_id}"
    qs = _month_events()
    qs = qs.filter(user_id=str(user_id)) if user_id else qs.filter(team_id=team_id)
    return _cached_sum(f"ai_spend_month_usd:{key_id}:{timezone.now():%Y%m}", qs, "cost_usd_micro")


def month_spend_jpy_milli() -> int:
    """This UTC month's ledger spend of the whole product, in milli-yen: the same sum
    `ai_cost_report` compares with `AI_MONTHLY_BUDGET_JPY`. Cached for 5 minutes."""
    return _cached_sum(
        f"ai_spend_month_jpy:all:{timezone.now():%Y%m}", _month_events(), "cost_jpy_milli"
    )


def _warn_once(scope: str, text: str, *args) -> None:
    """The alert that a limit is stopping AI, at most hourly per limit. WARNING, never ERROR: an
    ERROR on this logger turns a cron run red, and a reached limit is not a broken job."""
    if cache.add(f"ai_monthly_limit_logged:{scope}:{timezone.now():%Y%m}", 1, _LOG_EVERY_SECONDS):
        log.warning(text, *args)


def monthly_limit_reached(team_id: str | None = "") -> bool:
    """True when the product's monthly budget, or this team's monthly ceiling, is spent.

    Costs nothing while both settings are 0. Never raises.
    """
    try:
        cfg = settings.SEARCH_ENGINE
        budget_jpy = float(cfg.get("AI_MONTHLY_BUDGET_JPY", 0) or 0)
        ceiling_usd = float(cfg.get("AI_TEAM_MONTHLY_CEILING_USD", 0) or 0)
        if budget_jpy > 0:
            spent = month_spend_jpy_milli()
            if spent >= budget_jpy * 1000:
                _warn_once(
                    "all",
                    "AI monthly budget reached (%.0f of %.0f yen): AI features run plain until "
                    "the month ends.",
                    spent / 1000,
                    budget_jpy,
                )
                return True
        if ceiling_usd > 0 and team_id:
            spent = month_spend_usd_micro(team_id=str(team_id))
            if spent >= ceiling_usd * 1_000_000:
                _warn_once(
                    f"t:{team_id}",
                    "Team %s reached the monthly AI cost ceiling ($%.2f of $%.2f): its AI "
                    "features run plain until the month ends.",
                    team_id,
                    spent / 1_000_000,
                    ceiling_usd,
                )
                return True
        return False
    except Exception:  # noqa: BLE001 - a limit that cannot be read must not block
        log.warning("Monthly AI limit check failed", exc_info=True)
        return False

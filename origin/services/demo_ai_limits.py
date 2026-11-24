"""What AI a demo visitor may spend (MIP-2341).

`DemoSignInView` is open to anyone and makes a fresh `is_demo` user in a fresh Pro team on every
call, so without a limit of its own each visit got a new Pro allowance (70 credits, about $7) and
an open model picker (Claude Opus). Four limits, all server-side, all for `is_demo` users only:

1. A small FIXED credit budget (`DEMO_AI_CREDITS`), served by the ordinary credit ledger and gate
   (`credit_ledger.entitlement_for`), so it ends like any zero-credit case.
2. The default Gemini model and no more than the default effort (`DEMO_AI_MAX_EFFORT`), whatever
   `preferred_llm_*` holds (`agent_views._resolve_choice_for`, `quota.get_max_effort`).
3. A GLOBAL cap on demo asks per UTC day (`DEMO_AI_GLOBAL_DAILY_ASKS`): the backstop that does not
   depend on how many fresh users or IP addresses a caller has.
4. A cap per caller address per UTC day (`DEMO_AI_PER_IP_DAILY_ASKS`).

The two daily caps count in the `throttle` cache and FAIL CLOSED: with the counter store down a
demo ask is refused, because the point of the cap is that spend stays bounded when something is
wrong. A non-demo user never reaches the counter, so Redis trouble does not touch real customers.
The address is kept as a keyed marker (`ip_marker`), never raw, as in the throttles' log lines.

The same count is on every other surface where a demo user's action makes a paid model call, on
the cache-miss path only (MIP-2364): thread and note summaries, scheduled report sentences and
skill runs, and MCP `search_workspace` (which then runs plain). `/agent/decide/` resumes a run an
ask already counted, and Spotlight typeahead only embeds a cached query, so neither counts.

Not covered here: the monthly money ceilings (`AI_TEAM_MONTHLY_CEILING_USD`,
`AI_MONTHLY_BUDGET_JPY`) stay the operator's switch.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

log = logging.getLogger(__name__)

#: A demo visitor's whole AI allowance, in credits (1 credit = $0.10). Enforced through the same
#: credit gate as any plan, so it only bites while credits are authoritative.
DEMO_AI_CREDITS = 3

#: The highest effort a demo visitor can run at: the default one (`llm.choice.DEFAULT_EFFORT`).
DEMO_AI_MAX_EFFORT = "low"

#: The provider every demo ask is pinned to.
DEMO_AI_PROVIDER = "gemini"

#: Demo asks per UTC day across ALL demo users. At the default model a few cents each, so this
#: bounds the worst day for the demo as a whole.
DEMO_AI_GLOBAL_DAILY_ASKS = 300

#: Demo asks per UTC day from one caller address, however many demo users it signs in as.
DEMO_AI_PER_IP_DAILY_ASKS = 20

#: What a caller is told when a cap stops the ask. No number, no way round it, an honest next step.
DEMO_LIMIT_MESSAGE = (
    "The demo's AI is at its limit for today. Create a free account to keep using Genos AI."
)

_COUNTER_TTL = 2 * 24 * 3600


def is_demo_user(user_id: str | None) -> bool:
    """Whether `user_id` is a demo visitor. Read with the effective tier (one query, cached 60s,
    shared with every tier check), so it costs nothing extra on a path that reads the tier anyway.
    False for an unknown user or on any error: a lookup that cannot run must not turn a real
    customer into a demo user."""
    if not user_id:
        return False
    try:
        from origin.search_engine.quota import resolve_effective_tier  # noqa: PLC0415

        return bool(resolve_effective_tier(str(user_id)).get("is_demo"))
    except Exception:  # noqa: BLE001
        log.debug("demo user lookup failed", exc_info=True)
        return False


def _day() -> str:
    return timezone.now().strftime("%Y%m%d")


def _seconds_to_midnight_utc() -> int:
    now = timezone.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _hit(counter_cache, key: str) -> int:
    """Count one ask in `key` and return the new total."""
    if counter_cache.add(key, 1, _COUNTER_TTL):
        return 1
    try:
        return int(counter_cache.incr(key))
    except ValueError:
        counter_cache.set(key, 1, _COUNTER_TTL)
        return 1


def _refusal(reason: str) -> Response:
    retry = _seconds_to_midnight_utc()
    response = Response(
        {
            "error": DEMO_LIMIT_MESSAGE,
            "limit_reached": True,
            "category": "demo_limit",
            "reason": reason,
        },
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )
    response["Retry-After"] = str(retry)
    return response


def check_demo_ask(request, user) -> Response | None:
    """Count one AI ask by a demo user against the per-address and the global daily cap.

    Returns the 429 to send, or None when the ask may go on. None at once for anyone who is not a
    demo user (no cache access). Fails CLOSED for a demo user when the counter store errors."""
    if not getattr(user, "is_demo", False):
        return None

    from apis.log_redaction import ip_marker  # noqa: PLC0415
    from origin.services.security_events import client_ip  # noqa: PLC0415
    from origin.services.throttles import throttle_cache  # noqa: PLC0415

    day = _day()
    try:
        counter_cache = throttle_cache(breaker=False)
        # A call with no request (a cron run) has no caller address: only the global cap applies.
        if request is not None:
            marker = ip_marker(client_ip(request))
            if _hit(counter_cache, f"demo_ai:ip:{marker}:{day}") > DEMO_AI_PER_IP_DAILY_ASKS:
                log.warning("demo.ai_cap reason=ip client=%s", marker)
                return _refusal("ip")
        if _hit(counter_cache, f"demo_ai:global:{day}") > DEMO_AI_GLOBAL_DAILY_ASKS:
            log.warning("demo.ai_cap reason=global")
            return _refusal("global")
    except Exception:  # noqa: BLE001 — any counter failure refuses, for a demo user only
        log.error("demo.ai_cap counter unavailable: refusing the demo ask", exc_info=True)
        return _refusal("unavailable")
    return None


def demo_call_blocked(user_id: str | None) -> bool:
    """Count one model call that has no request (a scheduled report or skill run) for a demo user
    against the global daily cap. True = the cap stops it: the caller skips its AI step and files a
    plain result. False at once for anyone who is not a demo user (no counter access). Fails closed
    for a demo user, like `check_demo_ask`."""
    if not is_demo_user(user_id):
        return False

    class _Demo:
        is_demo = True

    return check_demo_ask(None, _Demo()) is not None

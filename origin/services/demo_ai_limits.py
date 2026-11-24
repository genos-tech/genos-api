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

Not covered here: the monthly money ceilings (`AI_TEAM_MONTHLY_CEILING_USD`,
`AI_MONTHLY_BUDGET_JPY`) stay the operator's switch.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
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

_IS_DEMO_TTL = 300
_COUNTER_TTL = 2 * 24 * 3600


def is_demo_user(user_id: str | None) -> bool:
    """Whether `user_id` is a demo visitor. Cached five minutes; False for an unknown user or on
    any error (a lookup that cannot run must not turn a real customer into a demo user)."""
    if not user_id:
        return False
    key = f"demo_user:{user_id}"
    try:
        cached = cache.get(key)
        if cached is not None:
            return bool(cached)
    except Exception:  # noqa: BLE001
        log.debug("demo user cache read failed", exc_info=True)
    try:
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        found = CustomUser.objects.filter(id=user_id, is_demo=True).exists()
    except Exception:  # noqa: BLE001
        log.debug("demo user lookup failed", exc_info=True)
        return False
    try:
        cache.set(key, found, _IS_DEMO_TTL)
    except Exception:  # noqa: BLE001
        pass
    return found


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
    marker = ip_marker(client_ip(request)) if request is not None else "-"
    try:
        counter_cache = throttle_cache(breaker=False)
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

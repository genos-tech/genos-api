"""Volume and reach limits per plan for the MCP server (MIP-1367, phase 3). DARK-SHIPPED.

What this is. The numbers a plan's MCP use is held to: calls per minute and per day, how many API
keys and connected apps a person may have, how many items one bulk call may carry, how big a page
of a list may be, and whether chat reads follow the plan's history window. They live in
`SEARCH_ENGINE["TIER_QUOTAS"]` beside every other tier limit, as capability keys, so tuning one is
a one-line change there (or a `TIER_QUOTAS_JSON` override) and the accessors here read them the way
`origin/search_engine/quota.py` reads its own.

What is NOT here. Safety caps (request size, result size, per-field text caps, title length, write
bodies) are the same for every plan and are plain constants of the phase 1 and 2 modules: a plan
override must never be able to raise them. Which tools a plan gets is not here either (MIP-1614).

THE SWITCH, `MCP_TIER_LIMITS_MODE` (read on every call, so an operator can change it with a
variable and no deploy):

  off      (default, and anything else)  Nothing at all: no counter, no query, no log line, no
           change to any answer. The code paths below return before touching anything.
  shadow   Computed against `MCP_TIER_LIMITS_PROPOSED` (the numbers Ken approved on 2026-11-15) and
           logged as `mcp.tier_would_limit tier= category= limit= used= tool=` (names and numbers,
           never a user, a team, an address or content). The call is answered exactly as without
           it. Shadow counters live under their own keys, so switching to `enforce` starts clean.
  enforce  The LIVE `TIER_QUOTAS` values apply. They are `None` (unlimited) for every plan until a
           person flips each one, so `enforce` with the table as shipped is the same as `off`
           (there is a test that proves it, and one that proves no counter is written).

WHEN A LOOKUP FAILS. A failure to resolve the person's plan uses Core's row, never "unlimited". A
MISSING key (a `TIER_QUOTAS_JSON` written before the key existed) is `None`, permissive, as the
file's other capabilities are: that is a configuration that says nothing, not a failure. A key
present with a value that cannot be a limit (a string, a negative number) is read as Core's value
for the key, and logged.

WHEN THE COUNTER STORE IS UNREACHABLE. The per-minute limit stays fail-open in the sense the
house policy means (a Redis hiccup must not take paying users down): it counts in this process
instead, against the plan's own number. The per-day and search totals count in this process
against CORE's numbers instead, the conservative end, because they bound paid work. In shadow
mode a store failure just skips the measurement.

Counting. A call's weight is 1; the heavy ones (`HEAVY_TOOLS`) weigh `HEAVY_WEIGHT` against both
the per-minute and the per-day counter. The per-minute counter is per CREDENTIAL (a key or a
connected app), the per-day counter and the search counter are per PERSON across every key and app.
A refused call is given back to every counter it had been added to, so `used` stays honest and one
refusal cannot cause the next.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings

from origin.search_engine import quota
from origin.services.throttles import CACHE_ERRORS, CacheDown, _trip_breaker, throttle_cache

log = logging.getLogger(__name__)

MODE_ENV = "MCP_TIER_LIMITS_MODE"
OFF, SHADOW, ENFORCE = "off", "shadow", "enforce"

# -- the capability keys (one per limit, in every tier's dict in apis/settings.py) ---------------
PER_MINUTE = "mcp_calls_per_min"
PER_DAY = "mcp_calls_per_day"
SEARCH_DAILY = "mcp_search_daily"
MAX_API_KEYS = "mcp_max_api_keys"
MAX_CONNECTIONS = "mcp_max_connections"
BULK_TASKS = "mcp_bulk_max_tasks"
BULK_PLAN_TASKS = "mcp_bulk_max_plan_tasks"
LIST_PAGE_MAX = "mcp_list_page_max"
# Not a number of days: whether MCP chat reads follow the plan's own message-history window
# (`message_retention_days`, which is LIVE for the app: Core 365, Pro and up unlimited). The days
# always come from `quota.get_message_retention_days`, the app's own per-viewer function, so there
# is one rule; this key is only the dark switch for applying it over MCP.
HISTORY = "mcp_apply_message_retention"

INT_KEYS = (
    PER_MINUTE,
    PER_DAY,
    SEARCH_DAILY,
    MAX_API_KEYS,
    MAX_CONNECTIONS,
    BULK_TASKS,
    BULK_PLAN_TASKS,
    LIST_PAGE_MAX,
)
CAPABILITY_KEYS = (*INT_KEYS, HISTORY)

# The `category` a client sees in `limit_reached` answers, one per key.
CATEGORY = {
    PER_MINUTE: "mcp_per_minute",
    PER_DAY: "mcp_per_day",
    SEARCH_DAILY: "mcp_search_daily",
    MAX_API_KEYS: "mcp_api_keys",
    MAX_CONNECTIONS: "mcp_connections",
    BULK_TASKS: "mcp_bulk_tasks",
    BULK_PLAN_TASKS: "mcp_bulk_plan_tasks",
    LIST_PAGE_MAX: "mcp_list_page",
    HISTORY: "mcp_history",
}

# What Ken approved on 2026-11-15 as the INITIAL numbers (he may tune them). Pinned by a test; the
# docs table is generated from `proposed_table_markdown()`. This is what `shadow` measures against
# and what a flip copies into `TIER_QUOTAS`, one key at a time. Free has no MCP, and its REST keys
# are not limited by an MCP-named cap, so it carries None throughout; Enterprise is unlimited.
MCP_TIER_LIMITS_PROPOSED: dict[str, dict[str, Any]] = {
    "free": dict.fromkeys(CAPABILITY_KEYS),
    "core": {
        PER_MINUTE: 60,
        PER_DAY: 3_000,
        SEARCH_DAILY: 100,
        MAX_API_KEYS: 3,
        MAX_CONNECTIONS: 3,
        BULK_TASKS: 10,
        BULK_PLAN_TASKS: 10,
        LIST_PAGE_MAX: 50,
        HISTORY: True,
    },
    "pro": {
        PER_MINUTE: 120,
        PER_DAY: 10_000,
        SEARCH_DAILY: 300,
        MAX_API_KEYS: 10,
        MAX_CONNECTIONS: 10,
        BULK_TASKS: 30,
        BULK_PLAN_TASKS: 20,
        LIST_PAGE_MAX: 100,
        HISTORY: True,
    },
    "max": {
        PER_MINUTE: 240,
        PER_DAY: 30_000,
        SEARCH_DAILY: 1_000,
        MAX_API_KEYS: 25,
        MAX_CONNECTIONS: 25,
        BULK_TASKS: 30,
        BULK_PLAN_TASKS: 20,
        LIST_PAGE_MAX: 100,
        HISTORY: True,
    },
    "enterprise": dict.fromkeys(CAPABILITY_KEYS),
}

# Calls that cost the server (and the person's model) far more than a read of one item: a briefing,
# a timeline, a workspace search, an item's line. They weigh this much against the per-minute and
# the per-day counter.
HEAVY_TOOLS = frozenset({"get_my_briefing", "get_timeline", "search_workspace", "get_item_line"})
HEAVY_WEIGHT = 5
# The tool the search counter is about (it embeds its query, which costs money).
SEARCH_TOOL = "search_workspace"

FALLBACK_TIER = "core"
_COUNTER_TTL_DAY = 2 * 24 * 3600
_COUNTER_TTL_MINUTE = 61
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _now() -> float:
    """The clock the windows are cut from (one place, so a test can move it)."""
    return time.time()


def mode() -> str:
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    return raw if raw in (SHADOW, ENFORCE) else OFF


def weight_of(tool_name: Any) -> int:
    return HEAVY_WEIGHT if isinstance(tool_name, str) and tool_name in HEAVY_TOOLS else 1


def _tool_label(tool_name: Any) -> str:
    """A tool name only when it has the shape of one: a log line never carries what a client sent."""
    if isinstance(tool_name, str) and _TOOL_NAME.fullmatch(tool_name):
        return tool_name
    return "-"


# -- the plan, and the number a plan has for a key -------------------------------------------------


def _valid(key: str, value: Any) -> bool:
    if value is None:
        return True
    if key == HISTORY:
        return isinstance(value, bool)
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _live_row(tier: str) -> dict[str, Any]:
    table = settings.SEARCH_ENGINE.get("TIER_QUOTAS") or {}
    return table.get(tier) or table.get(FALLBACK_TIER) or {}


def _row_value(row: dict[str, Any], key: str) -> Any:
    # A MISSING key is None (permissive), as for every other capability.
    return row.get(key)


@dataclass(frozen=True)
class Plan:
    """The limits of one person's effective tier for one call."""

    tier: str
    mode: str

    def _table_value(self, tier: str, key: str) -> Any:
        if self.mode == SHADOW:
            row = MCP_TIER_LIMITS_PROPOSED.get(tier) or MCP_TIER_LIMITS_PROPOSED[FALLBACK_TIER]
        else:
            row = _live_row(tier)
        return _row_value(row, key)

    def value(self, key: str) -> Any:
        """The limit for `key`: an int (or True/False for `HISTORY`), or None = unlimited."""
        raw = self._table_value(self.tier, key)
        if _valid(key, raw):
            return raw
        log.warning("MCP limit %s has a value that cannot be a limit; using Core's", key)
        core = self._table_value(FALLBACK_TIER, key)
        return core if _valid(key, core) else None

    def core_value(self, key: str) -> Any:
        core = self._table_value(FALLBACK_TIER, key)
        return core if _valid(key, core) else None


def plan_for(user_id: Any, active_mode: str | None = None) -> Plan:
    """The person's effective tier; Core's row when it cannot be resolved (never "unlimited")."""
    try:
        tier = quota.get_effective_tier(str(user_id))
    except Exception:  # noqa: BLE001 - a lookup failure must not fail the call, nor unlock it
        log.exception("MCP limits: tier lookup failed; using %s", FALLBACK_TIER)
        tier = FALLBACK_TIER
    return Plan(tier=tier or FALLBACK_TIER, mode=active_mode or mode())


# -- the log ---------------------------------------------------------------------------------------


def _log_line(event: str, plan: Plan, category: str, limit: Any, used: Any, tool: Any) -> None:
    log.info(
        "%s tier=%s category=%s limit=%s used=%s tool=%s",
        event,
        plan.tier,
        category,
        limit,
        used,
        _tool_label(tool),
    )


def _quiet_once(kind: str, ident: str, window: int) -> bool:
    """True when this (category, bucket, window) has not been logged yet. A person over a limit
    would otherwise write one line per call. A cache failure logs rather than hides."""
    try:
        return bool(throttle_cache().add(f"mcp_tl:logged:{kind}:{ident}:{window}", 1, 120))
    except CACHE_ERRORS:
        return True
    except Exception:  # noqa: BLE001
        return True


def note_would_limit(plan: Plan, category: str, limit: Any, used: Any, tool: Any, ident: str):
    if _quiet_once(category, ident, int(_now() // 60)):
        _log_line("mcp.tier_would_limit", plan, category, limit, used, tool)


def note_limited(plan: Plan, category: str, limit: Any, used: Any, tool: Any) -> None:
    _log_line("mcp.tier_limited", plan, category, limit, used, tool)


# -- counters --------------------------------------------------------------------------------------

_local_lock = threading.Lock()
_local: dict[str, tuple[float, int]] = {}
_LOCAL_PRUNE_AT = 5_000
_last_down_log = 0.0


def _local_bump(key: str, cost: int, ttl: int) -> int:
    """The in-process count, used while the shared store is down. Per worker, so it can only
    UNDER-count; it exists so a store outage is not a hole."""
    now = time.monotonic()
    with _local_lock:
        expires, count = _local.get(key, (0.0, 0))
        if expires <= now:
            expires, count = now + ttl, 0
        count = max(0, count + cost)
        _local[key] = (expires, count)
        if len(_local) > _LOCAL_PRUNE_AT:
            for stale in [k for k, (exp, _n) in _local.items() if exp <= now]:
                del _local[stale]
        return count


def _note_store_down(exc: BaseException) -> None:
    global _last_down_log
    if not isinstance(exc, CacheDown):
        _trip_breaker()
    now = time.monotonic()
    if now - _last_down_log > 60:
        _last_down_log = now
        log.warning("mcp.tier_limits store unavailable; counting in this process")


def _bump(key: str, cost: int, ttl: int, *, local_fallback: bool) -> tuple[int, bool] | None:
    """Add `cost` to a counter and return `(count, degraded)`. `degraded` means the shared store
    was unreachable and the count is this process's own. None when it was unreachable and the
    caller does not want a local count (shadow mode)."""
    try:
        cache = throttle_cache()
        if cache.add(key, cost, ttl):
            return cost, False
        try:
            return int(cache.incr(key, cost)), False
        except ValueError:
            # Expired between the add and the incr: the first call of what is left of the window.
            cache.set(key, cost, ttl)
            return cost, False
    except CACHE_ERRORS as exc:
        _note_store_down(exc)
    if not local_fallback:
        return None
    return _local_bump(key, cost, ttl), True


def _give_back(key: str, cost: int, degraded: bool) -> None:
    try:
        if degraded:
            _local_bump(key, -cost, 1)
        else:
            throttle_cache().incr(key, -cost)
    except Exception:  # noqa: BLE001 - a refund that fails only leaves the count slightly high
        pass


@dataclass(frozen=True)
class Refusal:
    """A limit that stopped a call: what to tell the client."""

    category: str
    limit: int
    used: int
    retry_after: int
    tier: str


def _seconds_to_next_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def admit_call(plan: Plan, *, user_id: Any, credential_ident: str, tool: Any) -> Refusal | None:
    """Count one `tools/call` against the per-minute, per-day and (for search) daily-search limits.

    Returns a `Refusal` when `enforce` stops it. In `shadow` it only logs. In `off` it is not
    called. A dimension whose limit is None is not counted at all: nothing is written for a plan
    that has no limit there.
    """
    if plan.mode == OFF:
        return None
    shadow = plan.mode == SHADOW
    ns = "s" if shadow else "e"
    cost = weight_of(tool)
    now_ts = _now()
    now = datetime.fromtimestamp(now_ts, UTC)
    minute = int(now_ts // 60)
    day = now.strftime("%Y%m%d")
    uid = str(user_id)

    # (limit key, bucket key, ttl, retry-after seconds, cost, bucket ident for the log)
    steps: list[tuple[str, str, int, int, int, str]] = []
    if plan.value(PER_MINUTE) is not None:
        steps.append(
            (
                PER_MINUTE,
                f"mcp_tl:{ns}:min:{credential_ident}:{minute}",
                _COUNTER_TTL_MINUTE,
                max(1, int((minute + 1) * 60 - now_ts)),
                cost,
                credential_ident,
            )
        )
    if plan.value(PER_DAY) is not None:
        steps.append(
            (
                PER_DAY,
                f"mcp_tl:{ns}:day:{uid}:{day}",
                _COUNTER_TTL_DAY,
                _seconds_to_next_utc_midnight(now),
                cost,
                uid,
            )
        )
    if tool == SEARCH_TOOL and plan.value(SEARCH_DAILY) is not None:
        steps.append(
            (
                SEARCH_DAILY,
                f"mcp_tl:{ns}:srch:{uid}:{day}",
                _COUNTER_TTL_DAY,
                _seconds_to_next_utc_midnight(now),
                1,
                uid,
            )
        )

    done: list[tuple[str, int, bool]] = []
    for key, bucket, ttl, retry, step_cost, ident in steps:
        counted = _bump(bucket, step_cost, ttl, local_fallback=not shadow)
        if counted is None:
            continue  # shadow, store down: skip the measurement
        count, degraded = counted
        limit = plan.value(key)
        if degraded and key != PER_MINUTE:
            # The totals fall back to CORE's number while the store is out (the conservative end).
            limit = plan.core_value(key)
        done.append((bucket, step_cost, degraded))
        if limit is None or count <= limit:
            continue
        if shadow:
            note_would_limit(plan, CATEGORY[key], limit, count - step_cost, tool, ident)
            continue
        for b, c, d in done:
            _give_back(b, c, d)
        note_limited(plan, CATEGORY[key], limit, count - step_cost, tool)
        return Refusal(CATEGORY[key], int(limit), count - step_cost, retry, plan.tier)
    return None


# -- caps on a size (bulk, page, key count, connection count) -------------------------------------


@dataclass(frozen=True)
class CapHit:
    category: str
    limit: int
    used: int
    tier: str


def check_cap(
    user_id: Any, key: str, *, size: int, used: int | None = None, tool: Any = None
) -> CapHit | None:
    """`size` against the person's limit for `key`. `off`: None without looking anything up.
    `shadow`: logs when it would have refused and returns None. `enforce`: a `CapHit` when
    `size` is over the limit. `used` is what a client is told it has (defaults to `size`)."""
    active = mode()
    if active == OFF:
        return None
    plan = plan_for(user_id, active)
    limit = plan.value(key)
    if limit is None or size <= limit:
        return None
    shown = size if used is None else used
    if active == SHADOW:
        note_would_limit(plan, CATEGORY[key], limit, shown, tool, f"u:{user_id}")
        return None
    note_limited(plan, CATEGORY[key], limit, shown, tool)
    return CapHit(CATEGORY[key], int(limit), shown, plan.tier)


def check_new_credential(user_id: Any, key: str, count: Callable[[], int]) -> CapHit | None:
    """May this person have ONE MORE key or connected app? For the creation of a NEW one only:
    the ones they already have keep working whatever the limit becomes (hide, never delete).

    `count` says how many they have now, and is called only when the plan has a limit here: `off`
    (and a plan with no limit) look nothing up. `shadow` logs `mcp.tier_would_limit` and answers
    None. A count that fails lets the creation through (an infra error never blocks the person)."""
    active = mode()
    if active == OFF:
        return None
    plan = plan_for(user_id, active)
    limit = plan.value(key)
    if limit is None:
        return None
    try:
        used = int(count())
    except Exception:  # noqa: BLE001 - never block a creation on a count that failed
        log.exception("MCP limits: counting for %s failed", key)
        return None
    if used + 1 <= limit:
        return None
    if active == SHADOW:
        note_would_limit(plan, CATEGORY[key], limit, used, None, f"u:{user_id}")
        return None
    note_limited(plan, CATEGORY[key], limit, used, None)
    return CapHit(CATEGORY[key], int(limit), used, plan.tier)


def page_cap(user_id: Any) -> int | None:
    """The largest page the person may ask for under `enforce`, or None. Shadow has no effect on
    a call, so it returns None too; `check_cap` is what logs a would-be refusal."""
    if mode() != ENFORCE:
        return None
    return plan_for(user_id, ENFORCE).value(LIST_PAGE_MAX)


# -- the history window ----------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryWindow:
    days: int
    cutoff: datetime
    tier: str
    enforced: bool


def history_window(user_id: Any) -> HistoryWindow | None:
    """The chat-history window MCP reads follow for this person, or None (no window).

    The days are the app's own (`get_message_retention_days`: per viewer, effective tier, None =
    unlimited, fails open to None). `shadow` returns the window with `enforced=False` so a caller
    can log what it WOULD have hidden and change nothing."""
    active = mode()
    if active == OFF:
        return None
    plan = plan_for(user_id, active)
    if not plan.value(HISTORY):
        return None
    days = quota.get_message_retention_days(str(user_id))
    if days is None:
        return None
    return HistoryWindow(
        days=int(days),
        cutoff=datetime.now(UTC) - timedelta(days=int(days)),
        tier=plan.tier,
        enforced=active == ENFORCE,
    )


def note_history(window: HistoryWindow, used: int, tool: Any, user_id: Any) -> None:
    """Log that a read touched history beyond the window: `used` = how many days back it reached
    (or how many messages were hidden, said by the caller's tool in the category line)."""
    plan = Plan(window.tier, ENFORCE if window.enforced else SHADOW)
    if window.enforced:
        note_limited(plan, CATEGORY[HISTORY], window.days, used, tool)
    else:
        note_would_limit(plan, CATEGORY[HISTORY], window.days, used, tool, f"u:{user_id}")


# -- the docs table --------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "unlimited"
    if value is True:
        return "follows plan"
    return f"{value:,}"


_ROW_LABELS = (
    (PER_MINUTE, "MCP calls per minute, per key or connected app"),
    (PER_DAY, "MCP calls per day, per person across all keys and apps"),
    (SEARCH_DAILY, "`search_workspace` calls per day"),
    (MAX_API_KEYS, "API keys per person (new ones only)"),
    (MAX_CONNECTIONS, "Connected AI apps per person (new ones only)"),
    (BULK_TASKS, "`update_tasks_bulk` items per call"),
    (BULK_PLAN_TASKS, "`create_task_plan` tasks per call"),
    (LIST_PAGE_MAX, "Largest list page a caller may ask for"),
    (HISTORY, "Chat history MCP reads can reach"),
)


def proposed_table_markdown() -> str:
    """The docs table, generated from `MCP_TIER_LIMITS_PROPOSED` so the two cannot drift."""
    tiers = ("core", "pro", "max", "enterprise")
    lines = [
        "| Limit | " + " | ".join(t.capitalize() for t in tiers) + " |",
        "| --- | " + " | ".join("---" for _ in tiers) + " |",
    ]
    for key, label in _ROW_LABELS:
        cells = [_cell(MCP_TIER_LIMITS_PROPOSED[t][key]) for t in tiers]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)

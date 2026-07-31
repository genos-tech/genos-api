"""Per-tier quota helpers.

A single `ModelUsageCounter` table holds counts for every metered
dimension, distinguished by the `model_name` field which acts as a
polymorphic key:

  - `"gemini-2.5-flash"`, `"claude-sonnet-4-6"`, ...
        → per-model agent ask count (daily).
  - `LLM_ASK_KEY = "__llm_ask__"`
        → daily total of agent asks (any model).
  - `WEB_SEARCH_KEY = "__web_search__"`
        → daily total of Tavily web searches.
  - `TASK_CREATE_KEY = "__task_create__"`
        → task creations, summed over the UTC calendar month.
  - `NOTE_CREATE_KEY = "__note_create__"`
        → note creations (personal + task + chat share one cap),
          summed over the UTC calendar month.

Monthly keys still write one row per (user, key, UTC day) — the month
usage is the SUM of the month's daily rows. Creations are what's
counted: deleting a resource never refunds quota (notes hard-delete,
so a COUNT(*) over source tables couldn't even see past creations).

Tier resolution: a user's *effective* tier is the best of their own
`CustomUser.tier` and the `plan` of every team they actively belong
to (one paying team upgrades all its members). Per-tier limits live
in `settings.SEARCH_ENGINE["TIER_QUOTAS"]`; the effective tier is
cached for 60s per user (see `get_effective_tier`).

Non-counter dimensions resolved from the same table of limits:
`message_retention_days` (chat history visibility window) and
`upload_max_mb` (per-file attachment ceiling).

Fail-open policy: quota checks and counter writes are best-effort —
an infra error (DB/Redis hiccup) must never block the user's actual
request. `check_remaining_monthly` returns "allowed" on any internal
error; `increment_usage` swallows and logs.

Race note: `(check_remaining*, increment_usage)` is not atomic. Two
concurrent requests at 9/10 both pass the pre-check and both
increment, yielding 11/10. Accepted for v1 — the over-count is at
most the worker's concurrent-request count and the next call still
gets blocked. If this matters, wrap the pair in `select_for_update`
inside `transaction.atomic`.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Sum
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.usage_models import ModelUsageCounter
from origin.models.common.user_models import CustomUser

log = logging.getLogger(__name__)

# Sentinel keys for the cross-dimensional counters in `ModelUsageCounter`.
# Chosen with leading + trailing underscores so they can never collide
# with a real model id from `MODEL_CATALOG`.
LLM_ASK_KEY = "__llm_ask__"
WEB_SEARCH_KEY = "__web_search__"
TASK_CREATE_KEY = "__task_create__"
NOTE_CREATE_KEY = "__note_create__"

# Sentinel key → TIER_QUOTAS config key for the monthly dimensions.
_MONTHLY_CFG_KEYS = {
    TASK_CREATE_KEY: "task_create_monthly",
    NOTE_CREATE_KEY: "note_create_monthly",
}

# Tier ladder for effective-tier resolution (higher wins).
_TIER_RANK = {"free": 0, "core": 1, "pro": 2, "max": 3, "enterprise": 4}

_EFFECTIVE_TIER_CACHE_SECONDS = 60
_EFFECTIVE_TIER_CACHE_PREFIX = "effective_tier:"


def get_user_tier(user_id: str) -> str:
    """Return the user's own tier ('free' | 'core' | 'pro' | 'max' | 'enterprise').

    This is the PERSONAL tier only — quota resolution should almost
    always go through `get_effective_tier`, which also considers team
    plans. Falls back to 'free' if the user can't be loaded —
    defensive against bad input; never raises.
    """
    try:
        tier = CustomUser.objects.filter(id=user_id).values_list("tier", flat=True).first()
    except Exception:  # noqa: BLE001
        log.exception("get_user_tier failed for user_id=%s", user_id)
        return "free"
    return tier or "free"


def resolve_effective_tier(user_id: str) -> dict:
    """Return `{"tier", "source", "team_name"}` for the user.

    `tier` is the best of the user's own `CustomUser.tier` and the
    `plan` of every non-deleted team they actively belong to.
    `source` is `"personal"` or `"team"`; `team_name` names the
    granting team when `source == "team"` (else None). Cached for 60s
    per user — tier changes propagate within a minute, which is
    accepted (`invalidate_effective_tier` shortens that for the CLI
    paths).
    """
    cache_key = f"{_EFFECTIVE_TIER_CACHE_PREFIX}{user_id}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and "tier" in cached:
        return cached

    best = get_user_tier(user_id)
    resolved = {"tier": best, "source": "personal", "team_name": None}
    try:
        team_plans = TeamMaster.objects.filter(
            team_members__attendee_id=user_id,
            team_members__is_deleted=False,
            is_deleted=False,
        ).values_list("plan", "team_name")
        for plan, team_name in team_plans:
            if _TIER_RANK.get(plan or "free", 0) > _TIER_RANK.get(resolved["tier"], 0):
                resolved = {"tier": plan, "source": "team", "team_name": team_name}
    except Exception:  # noqa: BLE001
        # Fail open to the personal tier — a team-lookup hiccup must
        # never downgrade a request to an error.
        log.exception("resolve_effective_tier team lookup failed for user_id=%s", user_id)

    cache.set(cache_key, resolved, _EFFECTIVE_TIER_CACHE_SECONDS)
    return resolved


def get_effective_tier(user_id: str) -> str:
    """Best of the user's own tier and their teams' plans."""
    return resolve_effective_tier(user_id)["tier"]


def invalidate_effective_tier(user_ids) -> None:
    """Best-effort cache eviction after a tier/plan change."""
    try:
        cache.delete_many([f"{_EFFECTIVE_TIER_CACHE_PREFIX}{uid}" for uid in user_ids])
    except Exception:  # noqa: BLE001
        log.exception("invalidate_effective_tier failed")


def _tier_cfg(tier: str) -> dict:
    """Return the TIER_QUOTAS dict for `tier`, or `free`'s if missing."""
    all_tiers = settings.SEARCH_ENGINE.get("TIER_QUOTAS") or {}
    return all_tiers.get(tier) or all_tiers.get("free") or {}


def _user_cfg(user_id: str) -> dict:
    return _tier_cfg(get_effective_tier(user_id))


def get_quota(user_id: str, key: str) -> int | None:
    """Return the quota for this user + counter key.

    Returns `None` to mean "no quota applies" (treated as unlimited at
    enforcement sites).

    Dispatch:
      - `LLM_ASK_KEY`     → `tier_cfg["llm_ask_daily"]`.
      - `WEB_SEARCH_KEY`  → `tier_cfg["web_search_daily"]`.
      - `TASK_CREATE_KEY` → `tier_cfg["task_create_monthly"]`.
      - `NOTE_CREATE_KEY` → `tier_cfg["note_create_monthly"]`.
      - any model id      → `tier_cfg["model_daily"].get(key)`.
    """
    cfg = _user_cfg(user_id)
    if key == LLM_ASK_KEY:
        v = cfg.get("llm_ask_daily")
    elif key == WEB_SEARCH_KEY:
        v = cfg.get("web_search_daily")
    elif key in _MONTHLY_CFG_KEYS:
        v = cfg.get(_MONTHLY_CFG_KEYS[key])
    else:
        v = (cfg.get("model_daily") or {}).get(key)
    if v is None:
        return None
    return int(v)


def get_used_today(user_id: str, key: str) -> int:
    """Today's (UTC) count for this user + key. 0 if no row yet."""
    today = timezone.now().date()
    row = (
        ModelUsageCounter.objects.filter(user_id=user_id, model_name=key, usage_date=today)
        .only("count")
        .first()
    )
    return int(row.count) if row else 0


def get_used_month(user_id: str, key: str) -> int:
    """This UTC calendar month's total for this user + key."""
    month_start = timezone.now().date().replace(day=1)
    agg = ModelUsageCounter.objects.filter(
        user_id=user_id, model_name=key, usage_date__gte=month_start
    ).aggregate(total=Sum("count"))
    return int(agg["total"] or 0)


def check_remaining(user_id: str, key: str) -> tuple[bool, int, int | None]:
    """Return (allowed, used_today, limit_or_None) for a DAILY key.

    - `allowed=True` when no quota applies (limit is None) or
      `used_today < limit`.
    - `allowed=False` when the quota is exhausted.
    """
    limit = get_quota(user_id, key)
    used = get_used_today(user_id, key)
    if limit is None:
        return True, used, None
    return used < limit, used, limit


def check_remaining_monthly(user_id: str, key: str, n: int = 1) -> tuple[bool, int, int | None]:
    """Return (allowed, used_this_month, limit_or_None) for a MONTHLY key.

    `n` is how many units the caller is about to consume — bulk
    creators (e.g. the create_task_plan agent tool) pass the batch
    size so a plan can't blow past the cap one approval at a time.

    Fail-open: any internal error returns (True, 0, None) — a counter
    outage must never block the user's write.
    """
    try:
        limit = get_quota(user_id, key)
        used = get_used_month(user_id, key)
        if limit is None:
            return True, used, None
        return used + n <= limit, used, limit
    except Exception:  # noqa: BLE001
        log.exception("check_remaining_monthly failed for user=%s key=%s", user_id, key)
        return True, 0, None


def increment_usage(user_id: str, key: str, amount: int = 1) -> None:
    """Atomically add `amount` to today's counter for (user, key).

    Failures are swallowed and logged — a counter write must never
    block the user's actual request.
    """
    today = timezone.now().date()
    try:
        obj, created = ModelUsageCounter.objects.get_or_create(
            user_id=user_id,
            model_name=key,
            usage_date=today,
            defaults={"count": amount},
        )
        if not created:
            ModelUsageCounter.objects.filter(pk=obj.pk).update(count=F("count") + amount)
    except Exception:  # noqa: BLE001
        log.exception("increment_usage failed for user=%s key=%s", user_id, key)


def get_message_retention_days(user_id: str) -> int | None:
    """Chat-history window (days) for the VIEWING user, or None = unlimited.

    Fail-open: errors return None (full history) — never hide data
    because of an infra hiccup.
    """
    try:
        v = _user_cfg(user_id).get("message_retention_days")
        return int(v) if v is not None else None
    except Exception:  # noqa: BLE001
        log.exception("get_message_retention_days failed for user=%s", user_id)
        return None


def get_upload_max_bytes(user_id: str) -> int | None:
    """Per-file upload ceiling in bytes, or None = no tier-specific limit.

    Callers fall back to their own absolute ceiling when None.
    Fail-open: errors return None.
    """
    try:
        v = _user_cfg(user_id).get("upload_max_mb")
        return int(v) * 1024 * 1024 if v is not None else None
    except Exception:  # noqa: BLE001
        log.exception("get_upload_max_bytes failed for user=%s", user_id)
        return None


# --- UX-pillar capability dimensions ---------------------------------
#
# The tier model's experience ladder (genos-docs
# operations/UX_TIER_MODEL_PLAN.md): what the agent may do, how deeply
# it thinks, what it remembers, what it can see, and whether it comes
# to you. Two contracts every accessor below honours:
#
#   * Fail-open: an infra error, a MISSING key (e.g. a
#     TIER_QUOTAS_JSON override written before the key existed), or an
#     unrecognized value all resolve to the PERMISSIVE default — a
#     Redis hiccup or a stale ops override must never silently
#     downgrade someone. (`digest_cadence` is the one deliberate
#     exception: on any doubt we do NOT send an unsolicited digest.)
#   * Normalization: enforcement sites only ever see values from the
#     declared vocabulary; anything else is logged and replaced.

_AGENT_TOOL_LEVELS = ("read", "act", "organize")
_EFFORT_LEVELS = ("low", "medium", "high")
_AGENT_MEMORY_LEVELS = ("none", "own", "team")
_ALL_INTEGRATIONS = ("web", "google_calendar", "github")
_DIGEST_CADENCES = ("weekly", "daily")


def _capability(user_id: str, key: str, default):
    """`_user_cfg` lookup under the fail-open contract above."""
    try:
        v = _user_cfg(user_id).get(key)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        log.exception("capability lookup failed for user=%s key=%s", user_id, key)
        return default


def _enum_capability(user_id: str, key: str, allowed: tuple, default: str) -> str:
    v = _capability(user_id, key, default)
    if v not in allowed:
        log.warning("TIER_QUOTAS %s=%r not in %s; using %r", key, v, allowed, default)
        return default
    return v


def get_agent_tool_level(user_id: str) -> str:
    """Agency ladder: 'read' (answers) | 'act' (single writes) |
    'organize' (composite writes). Permissive default: 'organize'."""
    return _enum_capability(user_id, "agent_tool_level", _AGENT_TOOL_LEVELS, "organize")


def get_max_effort(user_id: str) -> str:
    """Ceiling on the effort ladder: 'low' | 'medium' | 'high'.

    Enforcement CLAMPS a saved preference down to this — it never
    rejects the request (the preference outlives the subscription).
    Permissive default: 'high'.
    """
    return _enum_capability(user_id, "max_effort", _EFFORT_LEVELS, "high")


def get_auto_effort(user_id: str) -> bool:
    """Whether the tier may use the 'auto' (adaptive) effort router."""
    return bool(_capability(user_id, "auto_effort", True))


def get_agent_memory(user_id: str) -> str:
    """Memory lanes the agent may recall from: 'none' | 'own' (the
    user's past conversations) | 'team' (+ collected team answers).
    Permissive default: 'team'."""
    return _enum_capability(user_id, "agent_memory", _AGENT_MEMORY_LEVELS, "team")


def get_agent_history_retention_days(user_id: str) -> int | None:
    """Agent session history window (days), or None = unlimited.

    Windows are session-anchored (`last_active_at`) and HIDE, never
    delete — an upgrade restores full history. Fail-open: None.
    """
    try:
        v = _user_cfg(user_id).get("agent_history_retention_days")
        return int(v) if v is not None else None
    except Exception:  # noqa: BLE001
        log.exception("get_agent_history_retention_days failed for user=%s", user_id)
        return None


def get_integrations(user_id: str) -> list[str]:
    """Integration allowlist ('web' | 'google_calendar' | 'github').

    An explicit [] is a real restriction (Free); only a MISSING key or
    an error resolves to the full permissive list. Unknown names are
    kept — enforcement maps known names to tools, so they are inert.
    """
    v = _capability(user_id, "integrations", list(_ALL_INTEGRATIONS))
    if not isinstance(v, (list, tuple)):
        log.warning("TIER_QUOTAS integrations=%r is not a list; using all", v)
        return list(_ALL_INTEGRATIONS)
    return [str(x) for x in v]


def get_digest_cadence(user_id: str) -> str | None:
    """None (no digest) | 'weekly' | 'daily'.

    Deliberately fails CLOSED (None): a digest is unsolicited outbound,
    so on any infra doubt we stay silent rather than risk noise.
    """
    try:
        v = _user_cfg(user_id).get("digest_cadence")
    except Exception:  # noqa: BLE001
        log.exception("get_digest_cadence failed for user=%s", user_id)
        return None
    if v is None:
        return None
    if v not in _DIGEST_CADENCES:
        log.warning("TIER_QUOTAS digest_cadence=%r not in %s; off", v, _DIGEST_CADENCES)
        return None
    return v

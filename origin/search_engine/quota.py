"""Per-model daily quota helpers.

Wraps `ModelUsageCounter` reads and `F('count')+1` writes for the
agent-ask hot path. Tier resolution uses `UserFeatureAccess`
(`FEATURE_PAID_TIER` grant = "paid", absence = "free").

Race note: the `(check, increment)` pair is *not* atomic. Two
concurrent asks at 9/10 both pass the pre-check and both increment,
yielding 11/10. Accepted for v1 — the over-count is at most the
worker's concurrent-request count and the next call still gets
blocked. If this matters, wrap the pair in `select_for_update`
inside `transaction.atomic`.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from origin.models.common.feature_models import UserFeatureAccess
from origin.models.common.usage_models import ModelUsageCounter

log = logging.getLogger(__name__)


def get_user_tier(user_id: str) -> str:
    """Return the user's tier: 'paid' iff they have an active
    `FEATURE_PAID_TIER` grant, else 'free'.
    """
    if UserFeatureAccess.user_has(user_id, UserFeatureAccess.FEATURE_PAID_TIER):
        return "paid"
    return "free"


def get_quota(user_id: str, model_name: str) -> int | None:
    """Return the daily quota for this user + model, or `None` if
    unlimited.

    Resolution:
      - Look up the user's tier ('free' / 'paid') via
        `UserFeatureAccess`.
      - Read `SEARCH_ENGINE["MODEL_DAILY_QUOTAS"][tier][model_name]`.
      - Missing entry → no quota applies (`None`).
    """
    tier_quotas = (settings.SEARCH_ENGINE.get("MODEL_DAILY_QUOTAS") or {}).get(
        get_user_tier(user_id)
    )
    if not tier_quotas:
        return None
    if model_name not in tier_quotas:
        return None
    return int(tier_quotas[model_name])


def get_used_today(user_id: str, model_name: str) -> int:
    """Today's (UTC) ask count for this user + model. 0 if no row yet."""
    today = timezone.now().date()
    row = (
        ModelUsageCounter.objects.filter(user_id=user_id, model_name=model_name, usage_date=today)
        .only("count")
        .first()
    )
    return int(row.count) if row else 0


def check_remaining(user_id: str, model_name: str) -> tuple[bool, int, int | None]:
    """Return (allowed, used_today, limit_or_None).

    - `allowed=True` when no quota applies (limit is None) or
      `used_today < limit`.
    - `allowed=False` when the quota is exhausted.
    """
    limit = get_quota(user_id, model_name)
    used = get_used_today(user_id, model_name)
    if limit is None:
        return True, used, None
    return used < limit, used, limit


def increment_usage(user_id: str, model_name: str) -> None:
    """Atomically increment today's counter for this user + model.

    Uses `update_or_create` so concurrent first-of-the-day calls don't
    race the row's creation, and `F('count') + 1` on subsequent
    increments so the SQL is a single atomic UPDATE per call.
    """
    today = timezone.now().date()
    try:
        obj, created = ModelUsageCounter.objects.get_or_create(
            user_id=user_id,
            model_name=model_name,
            usage_date=today,
            defaults={"count": 1},
        )
        if not created:
            ModelUsageCounter.objects.filter(pk=obj.pk).update(count=F("count") + 1)
    except Exception:  # noqa: BLE001 — never fail the user's request on a counter write
        log.exception(
            "Failed to increment ModelUsageCounter for user=%s model=%s",
            user_id,
            model_name,
        )

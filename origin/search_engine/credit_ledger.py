"""Posting to, and reading from, the credit ledger — V2 layer 6.

`credits.py` is the pure arithmetic; this module is its Django-aware
counterpart: it knows about rows, periods, races and caches. Nothing
here decides a commercial number — amounts come in from the policy or
the caller, and this module's whole job is to make posting SAFE:

  * exactly-once, by CONSTRAINT rather than by locking. A charge posts
    at most once per logical request and a monthly grant at most once
    per (user, period, plan) because the database refuses the second
    row, not because callers remembered to serialize. The sprint
    bootstrap taught this codebase what lazy-write-on-read does under
    concurrency; the difference here is that a credit entry has natural
    unique keys, so `IntegrityError` IS the idempotency mechanism and
    `select_for_update` would add deadlock surface for nothing.
  * append-only, enforced by the model itself (`save`/`delete` raise).

Balances are a SUM over the period's entries, cached briefly —
consistent by construction, no counter to drift. Every read-side
failure returns the "unlimited" answer: shadow accounting must never
break or slow a request.

Periods are UTC calendar months ("2026-07"), matching the task/note
monthly quotas. The Stripe billing anchor is deliberately NOT consulted
— nothing local stores one, and no shadow-mode number depends on it;
revisit at Phase 2.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

log = logging.getLogger(__name__)

_BALANCE_CACHE_SECONDS = 60
_BALANCE_CACHE_PREFIX = "ai_credit_balance:"


def period_for(dt: datetime | None = None) -> str:
    """The UTC calendar month a moment belongs to, as 'YYYY-MM'."""
    dt = dt or timezone.now()
    return f"{dt.year:04d}-{dt.month:02d}"


def _policy():
    return getattr(settings, "CREDIT_POLICY", None)


def _invalidate_balance(user_id: str, period: str) -> None:
    try:
        cache.delete(f"{_BALANCE_CACHE_PREFIX}{user_id}:{period}")
    except Exception:  # noqa: BLE001
        log.debug("credit balance cache invalidation failed", exc_info=True)


def credits_authoritative() -> bool:
    """Credits are the customer's limit, replacing the daily ask count.

    Requires the shadow engine: without `AI_CREDITS_SHADOW` no charge is
    ever posted, so a balance would only ever go down by the monthly
    grant and every user would read as full forever. Enforcing on a
    ledger nobody writes to is worse than not enforcing.

    Public and single-sourced because two very different surfaces now
    branch on it — the ask gate, and the pricing page deciding whether
    to advertise credits or daily asks. Two copies of this predicate
    would eventually disagree, and the visible symptom would be a plans
    page selling a limit the server does not enforce.
    """
    se = settings.SEARCH_ENGINE
    return bool(se.get("AI_CREDITS_AUTHORITATIVE")) and bool(se.get("AI_CREDITS_SHADOW"))


def entitlement_milli(plan: str) -> int | None:
    """The plan's monthly grant in milli-credits; None = unlimited.

    An UNKNOWN plan gets free's entitlement — the safe floor. Silently
    treating a typo'd plan as unlimited would be the same failure mode
    as a model missing from `model_daily`.
    """
    policy = _policy()
    if policy is None:
        return None
    if plan in policy.entitlements_milli:
        return policy.entitlements_milli[plan]
    return policy.entitlements_milli.get("free", 0)


def ensure_monthly_grant(user_id: str, plan: str, *, period: str | None = None) -> None:
    """Materialize the month's grant for (user, plan), exactly once.

    Lazy — called on first balance read of a period. A mid-month
    UPGRADE posts a DELTA grant (new entitlement minus what monthly
    grants already sum to), as a new immutable row under the new plan;
    a downgrade posts nothing and takes nothing back. `get_effective_
    tier` is 60s-cached and one paying team member lifts a whole team,
    so plans genuinely move mid-period, and history must absorb that
    without being rewritten.

    Never raises; a grant that cannot post is a smaller failure than a
    request that cannot run.
    """
    if not user_id:
        return
    period = period or period_for()
    try:
        from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

        policy = _policy()
        target = entitlement_milli(plan)
        if policy is None or target is None:
            return  # unlimited (enterprise) — no entitlement accounting

        rows = AiCreditEntry.objects.filter(
            user_id=str(user_id),
            period=period,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_MONTHLY,
        )
        if rows.filter(plan=plan).exists():
            return
        already = int(rows.aggregate(s=Sum("credits_milli"))["s"] or 0)
        delta = target - already
        if delta <= 0:
            return  # downgrade, or an equal/greater plan already granted

        try:
            with transaction.atomic():
                AiCreditEntry.objects.create(
                    user_id=str(user_id),
                    entry_type=AiCreditEntry.ENTRY_GRANT,
                    kind=AiCreditEntry.KIND_MONTHLY,
                    credits_milli=delta,
                    period=period,
                    plan=plan,
                    credit_policy_version=policy.version,
                    plan_entitlement_version=policy.entitlement_version,
                    actor="system",
                    reason=(
                        f"monthly entitlement ({plan})"
                        if already == 0
                        else f"upgrade delta to {plan}"
                    ),
                )
        except IntegrityError:
            # A concurrent first-read won the race. The constraint is
            # the idempotency mechanism — this is the expected loss.
            return
        _invalidate_balance(str(user_id), period)
    except Exception:  # noqa: BLE001 — shadow accounting never breaks a request
        log.debug("ensure_monthly_grant failed", exc_info=True)


def balance_milli(user_id: str, plan: str, *, period: str | None = None) -> int | None:
    """The user's available milli-credits this period; None = unlimited.

    Grants are materialized lazily on first read, then the balance is
    one cached SUM. Fail-open: any error reads as unlimited, because a
    ledger hiccup must never block (or in shadow, mislabel) a request.
    """
    if not user_id:
        return None
    period = period or period_for()
    try:
        if entitlement_milli(plan) is None:
            return None
        cache_key = f"{_BALANCE_CACHE_PREFIX}{user_id}:{period}"
        cached = cache.get(cache_key)
        if cached is not None:
            return int(cached)

        ensure_monthly_grant(user_id, plan, period=period)

        from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

        total = int(
            AiCreditEntry.objects.filter(user_id=str(user_id), period=period).aggregate(
                s=Sum("credits_milli")
            )["s"]
            or 0
        )
        cache.set(cache_key, total, _BALANCE_CACHE_SECONDS)
        return total
    except Exception:  # noqa: BLE001
        log.debug("balance_milli failed", exc_info=True)
        return None


def post_charge(
    *,
    request_id: str,
    user_id: str,
    team_id: str = "",
    credits_milli: int,
    period: str | None = None,
    credit_policy_version: str = "",
    plan_entitlement_version: str = "",
) -> bool:
    """Post the final charge for one logical request. At most once.

    Returns True when a row was posted, False when it already existed
    (the partial unique constraint refused a duplicate — a re-close or
    a resumed leg re-finishing must not double-bill) or nothing was
    owed. Zero-credit outcomes post NOTHING: failed requests charging 0
    is provable from the rollup's `result`, and a ledger of mostly-zero
    lines would bury the entries that matter.
    """
    if credits_milli <= 0 or not request_id:
        return False
    period = period or period_for()
    try:
        from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

        try:
            with transaction.atomic():
                AiCreditEntry.objects.create(
                    user_id=str(user_id or ""),
                    team_id=str(team_id) if team_id else None,
                    entry_type=AiCreditEntry.ENTRY_CHARGE,
                    credits_milli=-int(credits_milli),
                    request_id=request_id,
                    period=period,
                    credit_policy_version=credit_policy_version,
                    plan_entitlement_version=plan_entitlement_version,
                    actor="system",
                )
        except IntegrityError:
            return False  # already charged — exactly-once, by constraint
        _invalidate_balance(str(user_id or ""), period)
        return True
    except Exception:  # noqa: BLE001
        log.debug("post_charge failed", exc_info=True)
        return False


def post_manual(
    *,
    user_id: str,
    credits_milli: int,
    reason: str,
    actor: str,
    kind: str = "manual",
    entry_type: str = "grant",
    ref_id: int | None = None,
    period: str | None = None,
) -> int:
    """Post a manual grant, adjustment or reversal. Returns the new id.

    The ops path (V2 gate 8): a genuine early user who ran out gets
    credits by command, with a reason and an actor on the row — the
    audit trail IS the row. Raises on failure: unlike request-path
    posting, an operator must see the error, not a silent no-op.
    """
    if not reason.strip() or not actor.strip():
        raise ValueError("manual credit entries require a reason and an actor")
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    policy = _policy()
    period = period or period_for()
    entry = AiCreditEntry.objects.create(
        user_id=str(user_id),
        entry_type=entry_type,
        kind=kind,
        credits_milli=int(credits_milli),
        period=period,
        ref_id=ref_id,
        reason=reason.strip()[:200],
        actor=actor.strip()[:64],
        credit_policy_version=policy.version if policy else "",
        plan_entitlement_version=policy.entitlement_version if policy else "",
    )
    _invalidate_balance(str(user_id), period)
    return entry.id


def reverse_entry(entry_id: int, *, reason: str, actor: str) -> int:
    """Reverse a posted entry by posting its negation. Returns new id.

    The correction path §3.6 allows: a bug that overcharged a user is
    fixed by RESTORING credits with an audit entry, never by touching
    the original row.
    """
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    original = AiCreditEntry.objects.get(id=entry_id)
    if original.entry_type == AiCreditEntry.ENTRY_REVERSAL:
        raise ValueError("refusing to reverse a reversal — post a fresh manual entry instead")
    if AiCreditEntry.objects.filter(
        entry_type=AiCreditEntry.ENTRY_REVERSAL, ref_id=original.id
    ).exists():
        raise ValueError(f"entry {entry_id} is already reversed")
    return post_manual(
        user_id=original.user_id,
        credits_milli=-original.credits_milli,
        reason=reason,
        actor=actor,
        kind=original.kind,
        entry_type=AiCreditEntry.ENTRY_REVERSAL,
        ref_id=original.id,
        period=original.period,
    )

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
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

log = logging.getLogger(__name__)

_BALANCE_CACHE_SECONDS = 60
# v2: the cached value changed from an int to a (monthly, purchased)
# pair. Reusing the prefix would let a warm cache from the previous
# release feed an int into a tuple unpack — which raises inside the broad
# `except` below and returns None, and None means UNLIMITED. Every user
# with a warm key would run uncapped until their entry aged out. A new
# prefix costs one cold read instead.
_BALANCE_CACHE_PREFIX = "ai_credit_balance2:"


def period_for(dt: datetime | None = None) -> str:
    """The UTC calendar month a moment belongs to, as 'YYYY-MM'."""
    dt = dt or timezone.now()
    return f"{dt.year:04d}-{dt.month:02d}"


def _policy():
    return getattr(settings, "CREDIT_POLICY", None)


def _invalidate_balance(user_id: str, period: str) -> None:
    """Drop the cached balance for `period` — and for the current month.

    Both, because purchased credits do not expire: a charge is stamped
    with the *request's* period, not now, so a request from last month
    settling today changes how much of the purchased bucket is left, and
    therefore what THIS month's balance reads. Dropping only the charged
    period would leave that stale for the cache's lifetime.
    """
    periods = {period, period_for()}
    for p in periods:
        try:
            cache.delete(f"{_BALANCE_CACHE_PREFIX}{user_id}:{p}")
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


@dataclass(frozen=True)
class Breakdown:
    """What a user has, split by where it came from.

    `total` is the only number enforcement uses; the two parts exist so
    the UI can say "40 this month + 100 you bought" rather than one
    figure that looks wrong next to a plan's advertised allowance.
    """

    monthly_milli: int
    purchased_milli: int

    @property
    def total_milli(self) -> int:
        return self.monthly_milli + self.purchased_milli


def balance_breakdown(user_id: str, plan: str, *, period: str | None = None) -> Breakdown | None:
    """The monthly and purchased buckets, separately. None = unlimited.

    **The spend order is derived, not recorded.** A charge row cannot say
    which bucket it drew from — `uq_credit_charge_per_request` allows one
    charge per request and `post_charge` takes no `kind` — so attributing
    a debit that spans both buckets is unrepresentable. It does not need
    to be: given the rule "monthly first", the split is a function of
    what is already stored.

        monthly_remaining(P) = max(0, monthly_granted(P) - charges(P))
        purchased_spent(P)   = max(0, charges(P) - monthly_granted(P))

    Charges in a period eat that period's allowance first and only then
    bite the purchased bucket, which is exactly the required order — and
    it falls out of the arithmetic rather than out of a field somebody
    has to remember to set.

    Expiry comes free from the same shape. The monthly part is read from
    the current period alone, so last month's unused allowance is simply
    not counted. The purchased part sums grants across ALL periods and
    subtracts what each period overspent, so it survives the rollover
    that expires the monthly one. That is why a purchased grant can keep
    carrying its purchase month in `period` — the column stays the
    expiry model for monthly credits and means nothing for these.

    Fail-open: any error reads as unlimited, because a ledger hiccup must
    never block (or in shadow, mislabel) a request.
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
            return Breakdown(int(cached[0]), int(cached[1]))

        ensure_monthly_grant(user_id, plan, period=period)

        from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

        rows = AiCreditEntry.objects.filter(user_id=str(user_id))

        # The split is on KIND, and everything not purchased is one
        # pool. That is not a shortcut — it is the only partition that
        # stays correct as row types are added. Charges carry `kind=""`,
        # and so do their reversals; monthly, promotional and manual
        # grants each belong to the month they were posted in.
        # Enumerating the members instead silently drops whatever is not
        # on the list: an operator's apology grant would land in neither
        # bucket and, worse, read as overspend that drains the user's
        # pack.
        #
        # It also puts a REVERSAL of a purchased grant on the purchased
        # side, where it belongs — `reverse_entry` copies the original's
        # kind, so refunding a pack posts `reversal`/`purchased`.
        monthly_remaining = max(0, _pool_delta_milli(rows, period))
        purchased_remaining = _purchased_remaining_milli(rows)

        cache.set(cache_key, (monthly_remaining, purchased_remaining), _BALANCE_CACHE_SECONDS)
        return Breakdown(monthly_remaining, purchased_remaining)
    except Exception:  # noqa: BLE001
        log.debug("balance_breakdown failed", exc_info=True)
        return None


def _sum(qs) -> int:
    return int(qs.aggregate(s=Sum("credits_milli"))["s"] or 0)


def _pool_delta_milli(rows, period: str) -> int:
    """What is left of one period's allowance: its grants minus its charges.

    Signed on purpose — a negative result is that period's **overspend**,
    the amount that had to come from somewhere other than the month's
    allowance.
    """
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    return _sum(rows.filter(period=period).exclude(kind=AiCreditEntry.KIND_PURCHASED))


def _purchased_remaining_milli(rows) -> int:
    """Purchased credits, less the overspend they have had to cover.

    **Only overspend from the first purchase onward counts**, and that
    bound is load-bearing rather than tidy. Credits have been running in
    SHADOW mode — `AI_CREDITS_AUTHORITATIVE` defaults to false, so
    nothing gates and a free account routinely charges many times its
    5-credit allowance. Counting all history would consume a customer's
    first pack to zero the moment they bought it: they pay, and receive
    nothing. Overspend that happened before they owned any purchased
    credits was never covered by them and must not be charged to them
    retroactively.

    `period` is a zero-padded "YYYY-MM", so a lexicographic `>=` is a
    chronological one.

    Returns 0 rather than a negative: a drained bucket is empty, not owed.
    """
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    purchased = rows.filter(kind=AiCreditEntry.KIND_PURCHASED)
    purchased_sum = _sum(purchased)
    if purchased_sum <= 0:
        # The overwhelmingly common path — nobody has bought anything.
        # Returning here keeps the grouped query below off the ask hot
        # path for every user who never will.
        return 0

    first_period = purchased.order_by("period").values_list("period", flat=True).first()
    if not first_period:
        return 0

    grouped = (
        rows.filter(period__gte=first_period)
        .exclude(kind=AiCreditEntry.KIND_PURCHASED)
        .values("period")
        .annotate(s=Sum("credits_milli"))
    )
    overspend = sum(max(0, -int(row["s"] or 0)) for row in grouped)
    return max(0, purchased_sum - overspend)


def balance_milli(user_id: str, plan: str, *, period: str | None = None) -> int | None:
    """Everything the user can spend right now; None = unlimited.

    The single scalar every enforcement site already reads. It is now the
    sum of two buckets (see `balance_breakdown`), which changes nothing
    for a caller that only asks "is there anything left".
    """
    breakdown = balance_breakdown(user_id, plan, period=period)
    return None if breakdown is None else breakdown.total_milli


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


def _pre_purchase_debt_milli(user_id: str, period: str) -> int:
    """Overshoot the buyer is carrying that a new pack must not inherit.

    Someone who overspends this month's allowance and THEN buys a pack
    would otherwise see the pack land already partly eaten — the
    overspend sits in the same period, so `_purchased_remaining_milli`
    counts it from the moment `first_period` becomes this month. Buying
    100 has to give 100.

    The amount forgiven is bounded by how far a single request may
    overshoot, and that overshoot is already forgiven at month end when
    the period rolls — this only brings the forgiveness forward.

    `- purchased_before` is what stops it becoming a gift to a repeat
    buyer: someone who legitimately spent an earlier pack has debt equal
    to that spending, and it must not be handed back to them when they
    buy again.
    """
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    rows = AiCreditEntry.objects.filter(user_id=str(user_id))
    purchased_before = _sum(rows.filter(kind=AiCreditEntry.KIND_PURCHASED))
    first_period = (
        rows.filter(kind=AiCreditEntry.KIND_PURCHASED)
        .order_by("period")
        .values_list("period", flat=True)
        .first()
        or period  # the FIRST purchase: only this month's overshoot counts
    )
    grouped = (
        rows.filter(period__gte=first_period)
        .exclude(kind=AiCreditEntry.KIND_PURCHASED)
        .values("period")
        .annotate(s=Sum("credits_milli"))
    )
    spent_before = sum(max(0, -int(row["s"] or 0)) for row in grouped)
    return max(0, spent_before - purchased_before)


def post_purchase(
    *,
    user_id: str,
    credits_milli: int,
    external_ref: str,
    team_id: str = "",
    reason: str = "",
    actor: str = "stripe",
) -> bool:
    """Grant purchased credits, at most once per `external_ref`.

    Returns True when a row was posted, False when this purchase had
    already been granted. Never raises — the caller is a webhook, and a
    500 back to Stripe buys a redelivery of an event we have already
    handled.

    **Exactly-once comes from the database**, via
    `uq_credit_grant_per_external_ref`, for the same reason a charge's
    does: the webhook layer's existing idempotency is convergence (set
    the tier — an assignment), and that reasoning does not survive an
    additive grant. Stripe delivers at least once; without the
    constraint a redelivery would grant the pack twice.

    `credits_milli` must come from the server's own policy, keyed by
    pack id — never from a quantity carried on the event, which is
    attacker-adjacent input.
    """
    if not user_id or not external_ref or int(credits_milli) <= 0:
        return False
    from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

    policy = _policy()
    period = period_for()

    # Debt the buyer already carries, computed BEFORE the grant exists so
    # it cannot see it. See `_pre_purchase_debt_milli` for why this is
    # required rather than generous.
    debt = _pre_purchase_debt_milli(str(user_id), period)

    try:
        with transaction.atomic():
            AiCreditEntry.objects.create(
                user_id=str(user_id),
                team_id=str(team_id or ""),
                entry_type=AiCreditEntry.ENTRY_GRANT,
                kind=AiCreditEntry.KIND_PURCHASED,
                credits_milli=int(credits_milli),
                # The purchase month. It is recorded because every row
                # carries one, NOT because it expires — `balance_breakdown`
                # sums purchased grants across all periods.
                period=period,
                external_ref=str(external_ref)[:128],
                reason=(reason or "credit pack")[:200],
                actor=(actor or "stripe")[:64],
                credit_policy_version=policy.version if policy else "",
                plan_entitlement_version=policy.entitlement_version if policy else "",
            )
            # The grant and its absorption row are ONE transaction,
            # grant first: a redelivery rolls back both, so the
            # absorption cannot be posted twice for one purchase.
            if debt > 0:
                AiCreditEntry.objects.create(
                    user_id=str(user_id),
                    entry_type=AiCreditEntry.ENTRY_GRANT,
                    kind=AiCreditEntry.KIND_MANUAL,
                    credits_milli=debt,
                    period=period,
                    reason=f"absorbed overshoot before {external_ref}"[:200],
                    actor="system",
                    credit_policy_version=policy.version if policy else "",
                    plan_entitlement_version=policy.entitlement_version if policy else "",
                )
    except IntegrityError:
        # Already granted. The expected outcome of a redelivery, not an
        # error — the constraint IS the idempotency mechanism.
        log.info("credit pack %s already granted; ignoring redelivery", external_ref)
        return False
    except Exception:  # noqa: BLE001 — a webhook must not 500 on our bookkeeping
        log.exception("post_purchase failed for %s", external_ref)
        return False
    _invalidate_balance(str(user_id), period)
    return True


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

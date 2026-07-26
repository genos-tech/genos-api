"""The Django half of the cost meter: `SpendRecord` -> `AiSpendEvent`.

Registered onto `llm/spend.py` at app-ready (`apps.py`). That module
holds the ContextVar and the neutral record type and imports nothing
from Django; this one owns pricing and persistence. Keeping them apart
is what lets the LLM adapters stay importable and testable with no
database, which is the same reason `llm/types.py` exists at all.

Pricing happens HERE, at write time, not in a report — the ledger's job
is to be reconcilable against a provider invoice months later, and that
is only possible if each row remembers the rate card and FX rate that
produced it.

Everything is best-effort. Accounting must never break generation: a
failure to record loses a row, a raise here would lose the user's
answer.
"""

from __future__ import annotations

import logging

from django.conf import settings

from origin.search_engine import credits
from origin.search_engine.llm import spend

log = logging.getLogger(__name__)

_BASIS_PRICED = "priced"
_BASIS_UNPRICED = "unpriced"
_BASIS_INCOMPLETE = "incomplete"


def _enabled() -> bool:
    return bool(settings.SEARCH_ENGINE.get("AI_COST_METER", False))


def _rate_card():
    catalog = getattr(settings, "LLM_CATALOG", None)
    return getattr(catalog, "rate_card", None) if catalog is not None else None


def price_llm_usage(usage) -> tuple[int, int, str]:
    """`(usd_micro, jpy_milli, basis)` for one LLM call's token usage.

    The four billable buckets, priced from the catalog by exact model
    id. `thought` folds into output (Gemini bills thinking at the output
    rate) and `tool_prompt` folds into input.

    Returns basis `unpriced` — with a cost of 0 — when the model has no
    rate. That 0 is MEANINGLESS and callers must never fold it into a
    total; it is reported as its own line instead. A model can be
    legitimately unpriced when an operator pins a preview id via env.
    """
    catalog = getattr(settings, "LLM_CATALOG", None)
    card = _rate_card()
    if catalog is None or card is None:
        return 0, 0, _BASIS_UNPRICED

    price = catalog.price_for(usage.model)
    if price is None:
        return 0, 0, _BASIS_UNPRICED

    prompt = max(int(usage.prompt_tokens), 0) + max(int(usage.tool_prompt_tokens), 0)
    cached = max(int(usage.cached_tokens), 0)
    cache_write = max(int(usage.cache_write_tokens), 0)
    output = max(int(usage.output_tokens), 0) + max(int(usage.thought_tokens), 0)

    usd = (
        prompt * price.input
        + cached * price.cached_input
        + cache_write * price.cache_write
        + output * price.output
    ) / 1_000_000.0

    # Integer micros/millis, never float storage — this ledger has to
    # reconcile against an invoice, and float sums drift unboundedly.
    usd_micro = int(round(usd * 1_000_000))
    jpy_milli = int(round(usd * float(card.fx_jpy_per_usd) * 1000))
    basis = _BASIS_PRICED
    if prompt == 0 and cached == 0 and output == 0 and cache_write == 0:
        # The provider never reported usage — the call died mid-stream,
        # or returned nothing. Distinguished from `priced` so the report
        # can show how much spend we know happened but cannot size.
        basis = _BASIS_INCOMPLETE
    return usd_micro, jpy_milli, basis


def price_units(unit_kind: str, provider: str, model: str, units: int) -> tuple[int, int, str]:
    """`(usd_micro, jpy_milli, basis)` for one per-unit-billed call.

    `units` is the PROVIDER'S billable quantity (Tavily credits, Cohere
    search units) — the call sites record it that way, so pricing is a
    straight multiply with no unit conversion hidden here.

    A zero-unit row with a known rate prices to 0 as `priced`, because
    that 0 is TRUE: a failed search is not billed by anyone. That is
    different from `unpriced`, whose 0 is meaningless — and it is what
    stops every ask that merely *used* the web from being flagged
    `has_unpriced` forever, which had made that flag mean nothing.
    """
    catalog = getattr(settings, "LLM_CATALOG", None)
    card = _rate_card()
    if catalog is None or card is None:
        return 0, 0, _BASIS_UNPRICED
    rate = catalog.unit_price_for(unit_kind, provider, model)
    if rate is None:
        return 0, 0, _BASIS_UNPRICED
    usd = max(int(units), 0) * rate
    usd_micro = int(round(usd * 1_000_000))
    jpy_milli = int(round(usd * float(card.fx_jpy_per_usd) * 1000))
    return usd_micro, jpy_milli, _BASIS_PRICED


def _credit_policy():
    return getattr(settings, "CREDIT_POLICY", None)


def _shadow_enabled() -> bool:
    """Shadow credits require the meter — no rollup, no charge."""
    return _enabled() and bool(settings.SEARCH_ENGINE.get("AI_CREDITS_SHADOW", False))


def _shadow_decision(ctx: spend.SpendContext) -> dict:
    """The V2 §3.5 quote/reserve flow, as recorded fact (layer 5).

    Returns the rollup fields for what an AUTHORITATIVE credit system
    would have done at this request's start: the quoted max, the
    balance as it stood, and whether the request would have been
    refused. Point-in-time by necessity — the balance moves with every
    request, so none of this is reconstructable later.

    `would_have_blocked` MIRRORS THE REAL GATE and has to keep doing so.
    It used to ask "did the balance cover the quote", which was the gate
    at the time; the gate now refuses only an exhausted balance and
    stops over-spend mid-run instead. Left as it was, this field would
    have counted every user below 5 credits as blocked — inflating the
    one number in `ai_credit_report` that the allowance decision is
    made on, in the direction that argues for raising allowances.

    In shadow there is nothing to actually reserve; recording
    balance-before and the quote IS the reservation, and the posted
    charge at settle is its release. Only billable surfaces read a
    balance (the read materializes the month's grant): quoting the
    search path would add a query to a surface that can never be
    charged.

    Never raises, and the empty dict is the honest default — a shadow
    field that silently defaulted to "would not have blocked" on an
    error would corrupt the one number Phase 1 exists to produce.
    """
    if not _shadow_enabled():
        return {}
    try:
        from origin.search_engine import credit_ledger, credits  # noqa: PLC0415

        policy = _credit_policy()
        if policy is None or ctx.surface not in policy.billable_surfaces:
            return {}
        quote = credits.quote_max_credits_milli(policy)
        balance = credit_ledger.balance_milli(ctx.user_id, ctx.plan)
        return {
            "quoted_max_credits_milli": quote,
            "balance_before_milli": balance,
            "would_have_blocked": balance is not None and balance <= 0,
        }
    except Exception:  # noqa: BLE001 — shadow accounting never breaks a request
        log.debug("shadow decision failed", exc_info=True)
        return {}


def finish_request(ctx: spend.SpendContext, *, result: str) -> None:
    """Close the rollup AND settle its shadow charge — the request
    lifecycle's terminal call.

    This is what entry points call when a logical request truly ends.
    `close_request` alone is the rollup derivation (what `--rebuild`
    replays); the ledger posting rides only on THIS function, so a
    rebuild is structurally incapable of re-posting history.
    """
    close_request(ctx, result=result)
    settle_shadow_charge(ctx)


def settle_shadow_charge(ctx: spend.SpendContext) -> None:
    """Post the request's final charge to the credit ledger.

    Reached only through `finish_request` (the request lifecycle's
    terminal call) — and deliberately NOT through `close_request`
    itself. `ai_cost_report --rebuild` re-derives rollups through
    `close_request`; if posting lived there, a rebuild under a newer
    policy would re-post history — the exact failure the append-only
    ledger exists to prevent, and `RebuildGuardTests` fails the moment
    someone moves it.

    Reads the closed rollup row rather than recomputing: the row IS the
    settled statement (result, credits, both policy versions), and the
    charge must match it exactly. The ledger's partial unique constraint
    makes a double-settle (resumed leg re-finishing, retried close) a
    no-op. Failed requests have shadow_credits 0 and post nothing.
    """
    if not _shadow_enabled():
        return
    try:
        from origin.search_engine import credit_ledger  # noqa: PLC0415
        from origin.search_engine.models import AiRequestCost  # noqa: PLC0415

        row = AiRequestCost.objects.filter(request_id=ctx.request_id).first()
        if row is None or row.shadow_credits_milli <= 0:
            return
        credit_ledger.post_charge(
            request_id=str(row.request_id),
            user_id=row.user_id,
            team_id=row.team_id or "",
            credits_milli=int(row.shadow_credits_milli),
            period=credit_ledger.period_for(row.started_at),
            credit_policy_version=row.credit_policy_version,
            plan_entitlement_version=row.plan_entitlement_version,
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to settle shadow charge", exc_info=True)


def record(rec: spend.SpendRecord) -> None:
    """Persist one `SpendRecord`. Never raises."""
    if not _enabled():
        return
    try:
        from origin.search_engine.models import AiSpendEvent  # noqa: PLC0415

        card = _rate_card()
        usage = rec.usage
        if usage is not None:
            # Note a failed call is still priced on whatever usage the
            # provider did report — a stream that died after emitting
            # output was billed for that output.
            usd_micro, jpy_milli, basis = price_llm_usage(usage)
            tokens = {
                "prompt_tokens": max(int(usage.prompt_tokens), 0),
                "cached_tokens": max(int(usage.cached_tokens), 0),
                "cache_write_tokens": max(int(usage.cache_write_tokens), 0),
                "output_tokens": max(int(usage.output_tokens), 0),
                "thought_tokens": max(int(usage.thought_tokens), 0),
                "tool_prompt_tokens": max(int(usage.tool_prompt_tokens), 0),
            }
        else:
            # Non-token spend, priced per unit from the catalog's
            # `unit_prices` table (exact (unit, provider, model) key, or
            # `unpriced`). These rates are published list prices, not
            # estimates — the "no estimates in the ledger" rule was
            # never meant to exclude them, and excluding them made the
            # cost of every ask that searched or reranked a silent
            # lower bound.
            usd_micro, jpy_milli, basis = price_units(
                rec.unit_kind, rec.provider, rec.model, rec.units
            )
            tokens = {}

        ctx = spend.current_context()
        if ctx is not None:
            ctx.cost_jpy_milli += jpy_milli

        AiSpendEvent.objects.create(
            request_id=rec.request_id,
            run_id=rec.run_id or None,
            user_id=rec.user_id,
            team_id=rec.team_id or None,
            surface=rec.surface,
            purpose=rec.purpose,
            plan=rec.plan,
            effort=rec.effort,
            provider=rec.provider,
            model=rec.model,
            attempt_no=rec.attempt_no,
            unit_kind=rec.unit_kind,
            units=rec.units,
            billable_units=getattr(rec, "billable_units", 0),
            cost_usd_micro=usd_micro,
            cost_jpy_milli=jpy_milli,
            cost_basis=basis,
            rate_card_version=card.version if card else "",
            fx_jpy_per_usd=float(card.fx_jpy_per_usd) if card else 0.0,
            latency_ms=rec.latency_ms,
            error=(rec.error or "")[:200],
            **tokens,
        )
    except Exception:  # noqa: BLE001 — accounting must never break generation
        log.debug("Failed to record AI spend event", exc_info=True)


def open_request(ctx: spend.SpendContext, *, quoted_max_jpy_milli: int = 0) -> None:
    """Write the request's rollup row at START, with its quote.

    The quote has to be written now: it is what we promised not to
    exceed, and after the fact there is no way to prove what it was.
    """
    if not _enabled():
        return
    try:
        from django.utils import timezone  # noqa: PLC0415

        from origin.search_engine.models import AiRequestCost  # noqa: PLC0415

        card = _rate_card()
        policy = _credit_policy()
        AiRequestCost.objects.update_or_create(
            request_id=ctx.request_id,
            defaults={
                "user_id": ctx.user_id,
                "team_id": ctx.team_id or None,
                "surface": ctx.surface,
                "plan": ctx.plan,
                "effort": ctx.effort,
                "quoted_max_jpy_milli": quoted_max_jpy_milli,
                "credit_policy_version": policy.version if policy else "",
                "plan_entitlement_version": policy.entitlement_version if policy else "",
                "rate_card_version": card.version if card else "",
                "started_at": timezone.now(),
                # The shadow decision is part of the OPEN, not the
                # close: quote and balance are what stood BEFORE any
                # spend, unprovable after the fact — the same argument
                # as quoted_max_jpy_milli.
                **_shadow_decision(ctx),
            },
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to open AiRequestCost", exc_info=True)


def close_request(ctx: spend.SpendContext, *, result: str) -> None:
    """Roll this request's events up onto its row.

    Derived from the EVENTS, not from the in-memory running total, so
    the rollup and the ledger cannot disagree. `charged` is 0 unless the
    provider actually performed the work, and is capped at the quote —
    the two customer-protection rules that have to be structural rather
    than remembered. "Performed the work" rather than "succeeded"
    because a run the credit balance cut short really did spend the
    tokens (`RESULTS_WORK_PERFORMED`).

    Two layers land here, deliberately distinct:

      * `charged`/`absorbed` — the COST meter's view, against the yen
        quote (`AI_REQUEST_MAX_JPY_MILLI`).
      * `eligible`/`shadow_credits` — the COMMERCIAL view (V2 layers
        3–4): what a customer could be asked to carry, derived by the
        pure functions in `credits.py` under `settings.CREDIT_POLICY`,
        whose two version ids are stamped alongside.

    This rollup is DERIVED and re-derivable (`--rebuild` recomputes it
    under the CURRENT policy). A posted credit charge is neither — it
    lives in its own append-only ledger, not here.
    """
    if not _enabled():
        return
    try:
        from django.db.models import Count, Sum  # noqa: PLC0415
        from django.utils import timezone  # noqa: PLC0415

        from origin.search_engine.models import AiRequestCost, AiSpendEvent  # noqa: PLC0415

        events = AiSpendEvent.objects.filter(request_id=ctx.request_id)
        agg = events.aggregate(
            n=Count("id"), jpy=Sum("cost_jpy_milli"), usd=Sum("cost_usd_micro")
        )
        computed_jpy = int(agg["jpy"] or 0)
        has_unpriced = events.filter(cost_basis__in=("unpriced", "incomplete")).exists()

        row = AiRequestCost.objects.filter(request_id=ctx.request_id).first()
        quote = int(row.quoted_max_jpy_milli) if row else 0

        if result in AiRequestCost.RESULTS_WORK_PERFORMED:
            charged = min(computed_jpy, quote) if quote > 0 else computed_jpy
        else:
            charged = 0  # a failed request is never charged

        policy = _credit_policy()
        if policy is not None:
            eligible = credits.eligible_jpy_milli(
                result=result,
                surface=ctx.surface,
                computed_jpy_milli=computed_jpy,
                policy=policy,
            )
            shadow = credits.credits_milli(eligible, policy)
        else:
            eligible, shadow = 0, 0

        AiRequestCost.objects.update_or_create(
            request_id=ctx.request_id,
            defaults={
                "run_id": ctx.run_id or None,
                "user_id": ctx.user_id,
                "team_id": ctx.team_id or None,
                "surface": ctx.surface,
                "plan": ctx.plan,
                "effort": ctx.effort,
                "result": result,
                "quoted_max_jpy_milli": quote,
                "computed_jpy_milli": computed_jpy,
                "computed_usd_micro": int(agg["usd"] or 0),
                "charged_jpy_milli": charged,
                "absorbed_jpy_milli": max(computed_jpy - charged, 0),
                "eligible_jpy_milli": eligible,
                "shadow_credits_milli": shadow,
                "credit_policy_version": policy.version if policy else "",
                "plan_entitlement_version": policy.entitlement_version if policy else "",
                "call_count": int(agg["n"] or 0),
                "has_unpriced": has_unpriced,
                "started_at": (row.started_at if row else timezone.now()),
                "finished_at": timezone.now(),
            },
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to close AiRequestCost", exc_info=True)


def install() -> None:
    """Wire this recorder into the neutral seam. Called from apps.ready()."""
    spend.set_recorder(record)

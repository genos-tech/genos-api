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

from origin.search_engine.llm import spend

log = logging.getLogger(__name__)

# Shadow-credit unit. ILLUSTRATIVE AND PROVISIONAL — the strategy doc
# explicitly defers the real number until per-request cost has been
# measured, and nothing enforces or displays credits at this stage.
# Stored per row so a later change doesn't restate history.
CREDIT_JPY = 2.0
CREDIT_POLICY_VERSION = "shadow-v0"

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


def shadow_credits_milli(jpy_milli: int) -> int:
    """Cost -> shadow credits, in MILLI-credits.

    Milli so a cheap request is not rounded up to a whole credit;
    fractional support is a stated requirement of the credit design and
    is a schema decision, not a display one.
    """
    if CREDIT_JPY <= 0:
        return 0
    return int(round(jpy_milli / CREDIT_JPY))


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
            # Non-token spend. No rate sheet for these yet, so they are
            # recorded with exact units and an explicit `unpriced`
            # basis rather than an invented per-unit price — an
            # estimate in a table whose whole value is that it isn't
            # one would be worse than a gap we can name.
            usd_micro, jpy_milli, basis = 0, 0, _BASIS_UNPRICED
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
        AiRequestCost.objects.update_or_create(
            request_id=ctx.request_id,
            defaults={
                "user_id": ctx.user_id,
                "team_id": ctx.team_id or None,
                "surface": ctx.surface,
                "plan": ctx.plan,
                "effort": ctx.effort,
                "quoted_max_jpy_milli": quoted_max_jpy_milli,
                "credit_policy_version": CREDIT_POLICY_VERSION,
                "rate_card_version": card.version if card else "",
                "started_at": timezone.now(),
            },
        )
    except Exception:  # noqa: BLE001
        log.debug("Failed to open AiRequestCost", exc_info=True)


def close_request(ctx: spend.SpendContext, *, result: str) -> None:
    """Roll this request's events up onto its row.

    Derived from the EVENTS, not from the in-memory running total, so
    the rollup and the ledger cannot disagree. `charged` is 0 for any
    non-success outcome and is capped at the quote — the two
    customer-protection rules that have to be structural rather than
    remembered.
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

        if result == AiRequestCost.RESULT_SUCCESS:
            charged = min(computed_jpy, quote) if quote > 0 else computed_jpy
        else:
            charged = 0  # a failed request is never charged

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
                "shadow_credits_milli": shadow_credits_milli(charged),
                "credit_policy_version": CREDIT_POLICY_VERSION,
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

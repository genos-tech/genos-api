"""Credit arithmetic — V2 layers 3 and 4, as PURE functions.

Layer 3 (`eligible_usd_micro`) narrows what we measured to what a
customer may be asked to carry. Layer 4 (`credits_milli`) converts that
to credits under a versioned policy. Both take every input as an
argument and read NOTHING at call time — no settings, no ORM, no clock.

The purity is load-bearing, not stylistic. V2 §5.1 requires comparing a
candidate policy against the live one by REPLAYING stored requests, and
§3.6 forbids recalculating posted charges — which together mean the
same stored inputs must produce the same outputs forever, under
whichever policy object is handed in. A function that peeked at
`settings.CREDIT_POLICY` would answer "what would this cost NOW" and
could never answer "what did we say it cost THEN".

Money in is **micro-USD**, credits out are **milli-credits**, both
integer. USD because that is what providers invoice, and because it
takes the exchange rate out of the credit path entirely: this used to
be usd ->(x150) jpy ->(/15) credits, which baked an FX assumption into
every posted charge and made what a credit MEANT drift with the yen.
Reports still read in ¥/€/£ — see `money.py` — but that is display,
downstream of everything here.

The seam these functions sit behind decides nothing commercial: every
rule here — which surfaces bill, which results bill, where the cap sits
— is DATA from `credit_policy.yaml`, so a rule change is a
fingerprint-moving config edit, never a silent code-path change.
"""

from __future__ import annotations

from apis.credit_policy import CreditPolicy

# Deliberately literals rather than imports: this module must stay
# importable with no Django configured. Both are pinned by tests against
# the `AiRequestCost` constants instead.
RESULT_SUCCESS = "success"
RESULT_CREDITS_EXHAUSTED = "credits_exhausted"


def eligible_usd_micro(
    *,
    result: str,
    surface: str,
    computed_usd_micro: int,
    policy: CreditPolicy,
) -> int:
    """What this request's customer-billable cost is, in micro-USD.

    The exclusions (V2 §3.3), in the order they short-circuit:

      * a non-billable surface is 0 — search, index, eval, judge and
        anything not explicitly listed in the policy. An UNKNOWN
        surface excludes itself, so forgetting to classify a new one
        fails customer-favorably;
      * a result outside `policy.billable_results` is 0 — provider
        failures, application failures, cancellations and safety
        refusals are our cost, structurally. Note this is a POLICY set,
        not "success": a run the balance cut short really did spend the
        tokens, and billing it 0 would make an empty balance the
        cheapest way to ask;
      * the per-request cap: cost above `request_max_credits` worth of
        USD is ours (`absorbed`), not the customer's.

    `computed_usd_micro` is the PRICED sum. When a request also carried
    unpriced calls the true cost is higher than computed — and the
    customer is billed the lower bound. That direction is deliberate:
    a metering gap must never overcharge.
    """
    if surface not in policy.billable_surfaces:
        return 0
    if result not in policy.billable_results:
        return 0
    eligible = max(int(computed_usd_micro), 0)
    return min(eligible, policy.request_max_usd_micro())


def credits_milli(eligible_usd_micro: int, policy: CreditPolicy) -> int:
    """Eligible micro-USD -> milli-credits under this policy.

    Milli so a cheap request is not rounded up to a whole credit —
    fractional credits are a stated design requirement, and rounding
    at storage time (rather than display time) is how drift becomes
    permanent.
    """
    if policy.credit_usd <= 0:
        return 0
    # micro-USD / (USD per credit) -> micro-credits -> milli-credits.
    return int(round(max(int(eligible_usd_micro), 0) / (policy.credit_usd * 1_000)))


def quote_max_credits_milli(policy: CreditPolicy) -> int:
    """The maximum credits a request may be charged — the shadow quote.

    Flat per-request for now, from the policy file. Deriving a
    per-class typical/max range (V2 §3.5's "Typical usage: X–Y") needs
    the measured distributions the benchmark suite produces; until
    those exist, any narrower quote would be hardcoded guesswork
    wearing a decimal point. The flat cap is still the true contract:
    actual cost above it is absorbed, never charged.
    """
    return policy.request_max_credits_milli


def request_budget_usd_micro(balance_credits_milli: int | None, policy: CreditPolicy) -> int:
    """How much this request may spend before it must stop, in micro-USD.

    The reservation half of V2 §3.5, and the reason a user is no longer
    refused for merely having less than the quote. Gating on
    `balance >= quote` made the last `request_max_credits` of every plan
    unspendable — on Free (10 credits, quote 5) that is HALF the
    allowance, dead. Instead a request starts on any positive balance
    and the agent loop stops itself when the running cost reaches this
    number.

    It is THE BALANCE, and deliberately not `min(balance, per-request
    cap)`. The cap governs what a request may be CHARGED, never how far
    it may RUN: cost above it lands in `absorbed_usd_micro`, which is
    the system saying out loud that we carry the excess. Capping the run
    too would silently truncate a long request from a user with plenty
    of credits — under a message telling them they had run out, which
    for that user is simply false. Bounding request LENGTH is what
    `AI_REQUEST_MAX_JPY_MILLI` is for, and it is a separate decision
    with its own copy.

    So the stop this feeds fires on exactly one condition: the user has
    genuinely spent everything they have. That keeps the message true
    whenever it appears, which a compound limit could not promise.

    Returns 0 ("no budget enforcement") for an unlimited plan, where
    there is no balance to run out of. 0 is also what every caller
    passes when credits are not authoritative, so the check is inert on
    the legacy path by construction rather than by a flag read.

    USD rather than credits because the running total the loop compares
    against (`SpendContext.cost_usd_micro`) is USD — converting once
    here beats converting on every step.
    """
    if balance_credits_milli is None:
        return 0
    # milli-credits x (USD per credit) -> milli-USD -> micro-USD.
    return int(round(max(int(balance_credits_milli), 0) * policy.credit_usd * 1_000))

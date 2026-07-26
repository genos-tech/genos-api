"""Credit arithmetic — V2 layers 3 and 4, as PURE functions.

Layer 3 (`eligible_jpy_milli`) narrows what we measured to what a
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

Everything is integer milli-units in and out (milli-yen, milli-
credits). The seam these functions sit behind decides nothing
commercial: every rule here — which surfaces bill, which results bill,
where the cap sits — is DATA from `credit_policy.yaml`, so a rule
change is a fingerprint-moving config edit, never a silent code-path
change.
"""

from __future__ import annotations

from apis.credit_policy import CreditPolicy

# The one result value that can carry a charge. Deliberately a literal
# rather than an import: this module must stay importable with no
# Django configured, and the value is pinned by a test against
# `AiRequestCost.RESULT_SUCCESS` instead.
RESULT_SUCCESS = "success"


def eligible_jpy_milli(
    *,
    result: str,
    surface: str,
    computed_jpy_milli: int,
    policy: CreditPolicy,
) -> int:
    """What this request's customer-billable cost is, in milli-yen.

    The exclusions (V2 §3.3), in the order they short-circuit:

      * a non-billable surface is 0 — search, index, eval, judge and
        anything not explicitly listed in the policy. An UNKNOWN
        surface excludes itself, so forgetting to classify a new one
        fails customer-favorably;
      * any result other than success is 0 — provider failures,
        application failures, cancellations and safety refusals are our
        cost, structurally;
      * the per-request cap: cost above `request_max_credits` worth of
        yen is ours (`absorbed`), not the customer's.

    `computed_jpy_milli` is the PRICED sum. When a request also carried
    unpriced calls the true cost is higher than computed — and the
    customer is billed the lower bound. That direction is deliberate:
    a metering gap must never overcharge.
    """
    if surface not in policy.billable_surfaces:
        return 0
    if result != RESULT_SUCCESS:
        return 0
    eligible = max(int(computed_jpy_milli), 0)
    return min(eligible, policy.request_max_jpy_milli())


def credits_milli(eligible_jpy_milli: int, policy: CreditPolicy) -> int:
    """Eligible milli-yen -> milli-credits under this policy.

    Milli so a cheap request is not rounded up to a whole credit —
    fractional credits are a stated design requirement, and rounding
    at storage time (rather than display time) is how drift becomes
    permanent.
    """
    if policy.credit_jpy <= 0:
        return 0
    return int(round(max(int(eligible_jpy_milli), 0) / policy.credit_jpy))


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

"""Stripe billing — self-serve personal subscriptions → `CustomUser.tier`.

The tier system (SUBSCRIPTION_TIERS.md) treats `CustomUser.tier` /
`TeamMaster.plan` as the single source of truth; this module is the
Stripe-facing layer that performs exactly the writes the
`feature_access` CLI does, driven by verified webhook events.

Design:
  * Checkout Session (mode=subscription) per plan — `core` / `pro` /
    `max` map to the price ids in `settings.STRIPE`. `enterprise` is
    contact-sales and never purchasable here. Prices are versioned:
    `PRICE_*` is what new checkouts are sold at, `PRICE_*_LEGACY` are
    retired ids that still resolve for grandfathered subscribers (see
    `tier_for_price`).
  * The TIER WRITE happens only in the webhook path — never on the
    success redirect (the redirect is unauthenticated evidence). The
    webhook verifies the `Stripe-Signature` header before anything
    else; unverified payloads are never parsed into actions.
  * `stripe_customer_id` is stored on first checkout (and again from
    the webhook, which is authoritative) so subscription lifecycle
    events resolve back to a user without trusting event metadata.
  * Lazy import + config gating: with `STRIPE_SECRET_KEY` unset (or
    the `stripe` package missing) everything degrades to
    `billing_enabled() == False` and the API returns 503s — the tier
    system itself keeps working, operator-managed.
  * Every tier write goes through `_set_personal_tier` /
    `_set_team_plan`, and they are the only two. Those check
    provenance: a tier an operator set by hand is not Stripe's to reset
    to free (SUBSCRIPTION_TIERS.md §8.3). A real subscription still
    wins and hands the account back to billing.

Event handling (see `handle_event`):
  * `checkout.session.completed` — bind customer id, set tier from the
    session metadata (validated against the price→tier map when
    present).
  * `customer.subscription.updated` / `.created` — status `active` /
    `trialing` → tier from the subscription's price id; terminal
    statuses (`canceled`, `unpaid`, `incomplete_expired`) → `free`;
    `past_due` keeps the current tier (dunning grace — Stripe retries,
    then fires a terminal status).
  * `customer.subscription.deleted` — → `free`. This is what fires at
    period end after "cancel at period end".

Every reset-to-free above is declined for an operator-set tier, and the
summary each arm returns is phrased from what the write RETURNED — it is
logged, echoed in the webhook's 200 body, and handed to the browser by
`/billing/refresh/`.

All writes are idempotent (set tier + evict the effective-tier cache),
so Stripe's at-least-once delivery needs no dedup table.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.cache import cache

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import (
    TIER_SET_BY_OPERATOR,
    TIER_SET_BY_STRIPE,
    CustomUser,
)
from origin.search_engine.quota import invalidate_effective_tier

log = logging.getLogger(__name__)

# Plans purchasable self-serve, cheapest first. Deliberately a subset
# of TIER_CHOICES (`free` isn't sold; `enterprise` is contact-sales).
PURCHASABLE_PLANS = ("core", "pro", "max")

# Grandfathered price ids → the plan they still grant. Populated from
# STRIPE_PRICE_*_LEGACY (comma-separated; blanks ignored). These are
# recognised on incoming subscription events but NEVER offered at
# checkout — see `price_for_plan` vs `tier_for_price` below.
_LEGACY_PRICE_SETTINGS = {
    "pro": "PRICE_PRO_LEGACY",
    "max": "PRICE_MAX_LEGACY",
}


def _legacy_price_ids(plan: str) -> list[str]:
    raw = settings.STRIPE.get(_LEGACY_PRICE_SETTINGS.get(plan, "")) or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


# Where Stripe sends the browser back to. MUST be a route inside the
# authenticated workspace: the app root is the guest-only sign-in
# route, so a signed-in user returning there is bounced to /jointeam by
# GuestGuard — dropping the ?billing= param, which silently skipped
# both the return toast and the tier reconcile. The plans page is also
# simply the right place to land: it shows the plan you just bought.
RETURN_PATH = "/workspace/plans"


class BillingError(Exception):
    """Billing is unconfigured, or a Stripe API call failed. The view
    layer maps this to a clean 4xx/5xx instead of a traceback."""


# Key types that can call the server-side API: secret and restricted.
# A publishable key (pk_) in STRIPE_SECRET_KEY is the classic dashboard
# copy-paste mix-up (the pk sits directly above the sk) — it sails past
# an is-it-set check and only explodes when a real user clicks Upgrade.
_SECRET_KEY_PREFIXES = ("sk_", "rk_")

# Warn-once dedup so the misconfiguration is loud in the logs without
# repeating on every `billing_enabled()` probe (config/plans fetches).
_bad_key_warned = ""


def _secret_key_error(secret: str) -> str | None:
    """None if `secret` looks like a server-side key, else the reason."""
    if not secret:
        return "Stripe billing is not configured."
    if not secret.startswith(_SECRET_KEY_PREFIXES):
        return (
            f"STRIPE_SECRET_KEY does not look like a secret key "
            f"(starts with {secret[:3]!r}; expected sk_ or rk_ — a pk_ "
            f"publishable key cannot call the Stripe API)."
        )
    return None


def _stripe():
    """Return the configured `stripe` module, or raise BillingError.

    Import is lazy (mirrors the tavily pattern in `web_search`) so the
    app boots fine without the package; the failure surfaces only when
    a billing endpoint is actually hit.
    """
    secret = settings.STRIPE.get("SECRET_KEY") or ""
    error = _secret_key_error(secret)
    if error:
        raise BillingError(error)
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        raise BillingError("The `stripe` package is not installed.")
    stripe.api_key = secret
    return stripe


def billing_enabled() -> bool:
    global _bad_key_warned
    secret = settings.STRIPE.get("SECRET_KEY") or ""
    if not secret:
        return False
    error = _secret_key_error(secret)
    if error:
        # Report disabled (no checkout buttons render) and say WHY once
        # — the alternative is a healthy-looking config endpoint and a
        # 503 in a user's face at click time.
        if _bad_key_warned != secret[:3]:
            _bad_key_warned = secret[:3]
            log.warning("stripe billing disabled: %s", error)
        return False
    try:
        import stripe  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def default_currency() -> str:
    """The currency a buyer gets when we have nothing better to go on."""
    return (settings.STRIPE.get("DEFAULT_CURRENCY") or "jpy").lower()


def supported_currencies() -> list[str]:
    """Currencies with at least one configured price, default first.

    Derived from configuration rather than declared, so a currency
    cannot be advertised on the pricing page before its Stripe prices
    exist — the failure that produces a checkout button leading to a
    dead end.
    """
    found = (
        {default_currency()}
        if any(price_for_plan(p, default_currency()) for p in PURCHASABLE_PLANS)
        else set()
    )
    for code in settings.STRIPE.get("PRICES_BY_CURRENCY") or {}:
        if any(price_for_plan(p, code) for p in PURCHASABLE_PLANS):
            found.add(code.lower())
    ordered = sorted(found - {default_currency()})
    return ([default_currency()] if default_currency() in found else []) + ordered


def price_for_plan(plan: str, currency: str | None = None) -> str | None:
    """The price a NEW checkout is sold at, in `currency`.

    Current prices only — never a grandfathered one, so nobody can buy
    into a retired price.

    Prices are DECLARED PER CURRENCY, never converted. `$9` and `¥1,200`
    are both deliberate round numbers chosen to read well locally;
    neither is the other one multiplied by an exchange rate. A converted
    subscription price gives you ¥1,847/month and moves every time the
    market does, which is why no SaaS does it — and it is the one place
    the "USD base + convert" rule of the cost system does NOT apply.

    Returns None when that plan has no price in that currency, which is
    what keeps `purchasable_plans` honest.
    """
    code = (currency or default_currency()).lower()
    if code == default_currency():
        return {
            "core": settings.STRIPE.get("PRICE_CORE") or None,
            "pro": settings.STRIPE.get("PRICE_PRO") or None,
            "max": settings.STRIPE.get("PRICE_MAX") or None,
        }.get(plan)
    by_currency = (settings.STRIPE.get("PRICES_BY_CURRENCY") or {}).get(code) or {}
    return by_currency.get(plan) or None


def tier_for_price(price_id: str | None) -> str | None:
    """Reverse map: Stripe price id → tier name. None for unknown ids
    (e.g. a price created in the dashboard but not wired into env) —
    callers log-and-ignore rather than guessing.

    Deliberately MANY-to-one: current prices AND grandfathered ones both
    resolve, because Stripe never migrates an existing subscription onto
    a new price object. After the repricing, an existing subscriber's
    renewal still carries the price id they originally bought — if only
    the current ids resolved here, every one of those renewals would log
    "unmapped price" and quietly stop maintaining their tier.

    Current ids are checked first so that if an id is somehow listed as
    both, the live price wins.

    Checks EVERY configured currency, not just the default. A euro
    subscriber's renewal carries the euro price id, and if only the
    default currency's ids resolved here, every renewal outside Japan
    would log "unmapped price" and quietly stop maintaining that
    customer's tier — the same failure the grandfathered ids above exist
    to prevent, one axis over.
    """
    if not price_id:
        return None
    for code in supported_currencies():
        for plan in PURCHASABLE_PLANS:
            if price_for_plan(plan, code) == price_id:
                return plan
    for plan in PURCHASABLE_PLANS:
        if price_id in _legacy_price_ids(plan):
            return plan
    return None


def purchasable_plans(currency: str | None = None) -> list[str]:
    """Plans with a configured price in `currency` — what the frontend
    may offer. A plan priced in yen but not yet in euros is simply not
    offered to a euro buyer, rather than offered and then failing at
    checkout."""
    return [p for p in PURCHASABLE_PLANS if price_for_plan(p, currency)]


def price_display(plan: str, currency: str | None = None) -> dict | None:
    """`{"amount", "currency", "interval"}` for a purchasable plan,
    read from its Stripe price so the page can never advertise an
    amount Stripe won't charge. Cached for an hour, and fail-SOFT:
    billing disabled, unmapped plan, or a Stripe error all return None
    — the plans page then renders limits without a price line rather
    than failing. `amount` is in the currency's smallest unit as Stripe
    stores it (JPY is zero-decimal: 1200 == ¥1,200).

    The `currency` field comes back from STRIPE, not from the argument:
    the price object is the authority on what a customer will actually
    be charged, and echoing the request would let a misconfigured price
    id advertise the wrong currency."""
    price_id = price_for_plan(plan, currency)
    if not price_id or not billing_enabled():
        return None
    cache_key = f"stripe_price_display:{price_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        stripe = _stripe()
        price = json.loads(str(stripe.Price.retrieve(price_id)))
        out = {
            "amount": price.get("unit_amount"),
            "currency": price.get("currency"),
            "interval": ((price.get("recurring") or {}).get("interval")) or "month",
        }
    except Exception as e:  # noqa: BLE001
        log.warning("stripe price lookup failed for plan %s: %s", plan, e)
        return None
    cache.set(cache_key, out, 3600)
    return out


def _set_personal_tier(
    user: CustomUser,
    tier: str,
    *,
    reason: str,
    source: str = TIER_SET_BY_STRIPE,
) -> bool:
    """The same write `feature_access set-tier` performs, attributed.

    Returns True when the tier actually moved — callers phrase their
    summary from it, because a Stripe-sourced write can be DECLINED here
    and a summary that says "tier set to free" next to a row that still
    says `pro` is how a support ticket becomes an hour of log reading.

    The one thing declined: a Stripe write to `free` against an
    operator-set tier. `free` is precisely the removal — it is the only
    tier Stripe ever writes that isn't a live subscription's plan
    (`tier_for_price` returns only PURCHASABLE_PLANS) — and an operator's
    comp is not Stripe's to remove. Every other Stripe write is a real
    subscription, which applies AND returns the account to billing
    management. See TIER_SET_BY_CHOICES for why that asymmetry is the
    point rather than an inconsistency.
    """
    previous = user.tier or "free"
    previous_set_by = user.tier_set_by or TIER_SET_BY_STRIPE
    if source == TIER_SET_BY_STRIPE and previous_set_by == TIER_SET_BY_OPERATOR and tier == "free":
        log.info(
            "stripe billing: tier for %s stays %s — operator-set, not Stripe's to remove (%s)",
            user.email,
            previous,
            reason,
        )
        return False

    updates: dict[str, str] = {}
    if previous != tier:
        updates["tier"] = tier
    # Recorded even when the tier itself doesn't move: someone comped at
    # `pro` who then subscribes to `pro` must lose the pin, or they keep
    # the tier for free the day they cancel.
    if previous_set_by != source:
        updates["tier_set_by"] = source
    if not updates:
        return False

    for field, value in updates.items():
        setattr(user, field, value)
    user.save(update_fields=list(updates))
    if "tier" in updates:
        invalidate_effective_tier([user.id])
    log.info(
        "stripe billing: tier for %s: %s -> %s [source %s -> %s] (%s)",
        user.email,
        previous,
        tier,
        previous_set_by,
        source,
        reason,
    )
    return "tier" in updates


def _bind_customer(user: CustomUser, customer_id: str | None) -> None:
    if customer_id and user.stripe_customer_id != customer_id:
        user.stripe_customer_id = customer_id
        user.save(update_fields=["stripe_customer_id"])


def _customer_is_alive(stripe, customer_id: str) -> bool:
    """True when the customer exists and isn't deleted.

    False ONLY on the two definitive gone-signals: Stripe returns the
    `{"deleted": true}` stub (customer was deleted in the dashboard) or
    a `resource_missing` error (id never existed / wrong mode). Any
    OTHER failure raises instead — minting a replacement customer on a
    transient error would silently detach the user from the customer
    their live subscription bills against, and later webhooks for that
    subscription would no longer resolve to them.
    """
    try:
        customer = json.loads(str(stripe.Customer.retrieve(customer_id)))
    except Exception as e:  # noqa: BLE001
        if getattr(e, "code", "") == "resource_missing":
            return False
        raise BillingError(f"Could not verify the Stripe customer: {e}")
    return not customer.get("deleted", False)


def ensure_customer(user: CustomUser) -> str:
    """Return a USABLE Stripe customer id for this user, creating one
    when needed. The customer carries our user id in metadata for
    dashboard-side debugging; the authoritative link is the
    `stripe_customer_id` column.

    A stored id is verified against Stripe first: a customer deleted in
    the dashboard used to brick that user's checkout permanently (every
    session create failed `resource_missing` until an operator nulled
    the column). Definitively-gone customers now just get replaced.
    """
    stripe = _stripe()
    if user.stripe_customer_id:
        if _customer_is_alive(stripe, user.stripe_customer_id):
            return user.stripe_customer_id
        log.warning(
            "stripe customer %s for %s no longer exists — minting a replacement",
            user.stripe_customer_id,
            user.email,
        )
    try:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.username or "",
            metadata={"genos_user_id": str(user.id)},
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not create Stripe customer: {e}")
    _bind_customer(user, customer["id"])
    return customer["id"]


def create_checkout_session(user: CustomUser, plan: str, currency: str | None = None) -> str:
    """Create a subscription Checkout Session; return its redirect URL.

    `currency` selects WHICH declared price is sold, not a conversion —
    the price object already carries its own currency, so Stripe charges
    exactly the round number a human chose for that market.
    """
    if plan not in PURCHASABLE_PLANS:
        raise BillingError(f"Unknown plan {plan!r}.")
    price_id = price_for_plan(plan, currency)
    if not price_id:
        raise BillingError(
            f"Plan {plan!r} has no configured Stripe price in "
            f"{(currency or default_currency()).upper()}."
        )
    stripe = _stripe()
    customer_id = ensure_customer(user)
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    params = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        # The webhook resolves the user from this — never from
        # anything the browser can influence.
        "client_reference_id": str(user.id),
        "metadata": {"genos_user_id": str(user.id), "plan": plan},
        "success_url": f"{base}{RETURN_PATH}?billing=success&plan={plan}",
        "cancel_url": f"{base}{RETURN_PATH}?billing=cancelled",
        # Stripe Tax: activates only when enabled on the account
        # (dashboard: Settings → Tax). Harmless flag otherwise per
        # Stripe docs; if account setup is incomplete Stripe returns
        # a clear error at session-create time, surfaced as a 503.
        "automatic_tax": {"enabled": settings.STRIPE.get("AUTOMATIC_TAX", False)},
    }
    # "I agree to the terms" checkbox on the Stripe-hosted page. The
    # linked terms URL comes from the ACCOUNT (dashboard: Settings →
    # Business → Public details → terms of service = the /legal page,
    # which carries the cancellation/refund policy). Gated because
    # Stripe rejects session creation when the URL isn't configured.
    if settings.STRIPE.get("TOS_CONSENT"):
        params["consent_collection"] = {"terms_of_service": "required"}
    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not start checkout: {e}")
    return session["url"]


# --------------------------------------------------------------------------- #
# Credit packs — one-off purchases                                            #
# --------------------------------------------------------------------------- #
#
# Everything below is deliberately parallel to the subscription code
# above but shares none of its lookups, because the two must not be able
# to reach each other's price maps. `tier_for_price` scans the plan
# prices to decide which tier a paid price represents; a pack price
# visible to it would make buying credits grant a subscription.


def credit_pack_price(pack: str, currency: str | None = None) -> str:
    """The Stripe price id for a pack in a currency, or "" if unsold there."""
    code = (currency or default_currency()).lower()
    return (settings.STRIPE.get("CREDIT_PACK_PRICES") or {}).get(code, {}).get(pack, "")


def credit_pack_credits_milli(pack: str) -> int:
    """What the pack grants, from the versioned policy — never from the event.

    The webhook must not read a quantity off anything the buyer's
    browser touched, so the amount is looked up here by pack id and the
    id is the only thing that travels.
    """
    policy = getattr(settings, "CREDIT_POLICY", None)
    if policy is None:
        return 0
    return int(getattr(policy, "credit_packs_milli", {}).get(pack, 0))


def purchasable_credit_packs(currency: str | None = None) -> list[str]:
    """Packs sellable in this currency, largest first.

    Both halves must be present: a price in this currency AND an amount
    in the policy. Either alone is a misconfiguration that would take
    money for nothing or offer a pack that cannot be charged, so it is
    simply not offered — the same rule `purchasable_plans` follows.
    """
    priced = (settings.STRIPE.get("CREDIT_PACK_PRICES") or {}).get(
        (currency or default_currency()).lower(), {}
    )
    packs = [p for p in priced if credit_pack_credits_milli(p) > 0 and priced[p]]
    return sorted(packs, key=credit_pack_credits_milli, reverse=True)


def credit_pack_price_display(pack: str, currency: str | None = None) -> dict | None:
    """`{amount, currency}` for a pack, read from Stripe and cached 1h.

    Mirrors `price_display` for plans, including its fail-soft None: a
    Stripe hiccup should grey out a price, not take the page down.
    """
    price_id = credit_pack_price(pack, currency)
    if not price_id:
        return None
    if not billing_enabled():
        return None
    cache_key = f"stripe_price_display:{price_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        stripe = _stripe()
        price = json.loads(str(stripe.Price.retrieve(price_id)))
        # No `interval` — a pack is a one-off, and echoing "month" here
        # would put "/month" beside a price that recurs never.
        out = {"amount": price.get("unit_amount"), "currency": price.get("currency")}
    except Exception as e:  # noqa: BLE001
        log.warning("stripe price lookup failed for credit pack %s: %s", pack, e)
        return None
    cache.set(cache_key, out, 3600)
    return out


def create_credit_pack_checkout_session(
    user: CustomUser, pack: str, currency: str | None = None
) -> str:
    """Create a ONE-OFF Checkout Session for a credit pack; return its URL."""
    if credit_pack_credits_milli(pack) <= 0:
        raise BillingError(f"Unknown credit pack {pack!r}.")
    price_id = credit_pack_price(pack, currency)
    if not price_id:
        raise BillingError(
            f"Credit pack {pack!r} has no configured Stripe price in "
            f"{(currency or default_currency()).upper()}."
        )
    stripe = _stripe()
    customer_id = ensure_customer(user)
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    params = {
        # NOT "subscription" — this is the only one-off purchase in the
        # product, and the difference is the whole point: it must never
        # create a recurring charge.
        "mode": "payment",
        # CARD ONLY, by decision (owner, 2026-08-03) — never the
        # account's default payment-method list. Konbini and bank
        # transfer complete a session with `payment_status: "unpaid"`
        # and settle days later; for a digital good delivered instantly
        # that gap is all downside, so those methods are simply not
        # offered. The deferred-payment machinery further down
        # (`payment_status` guard, the async_payment_succeeded branch)
        # is therefore defense in depth, not a mainline path.
        "payment_method_types": ["card"],
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "client_reference_id": str(user.id),
        # ⚠️ NO `plan` KEY. `handle_event` reads `metadata.plan` off a
        # completed session and sets that subscription tier outright, so
        # a pack carrying one would hand out a paid plan for the price of
        # a credit pack.
        "metadata": {"genos_user_id": str(user.id), "genos_credit_pack": pack},
        "success_url": f"{base}{RETURN_PATH}?billing=credits",
        "cancel_url": f"{base}{RETURN_PATH}?billing=cancelled",
        "automatic_tax": {"enabled": settings.STRIPE.get("AUTOMATIC_TAX", False)},
    }
    if settings.STRIPE.get("TOS_CONSENT"):
        params["consent_collection"] = {"terms_of_service": "required"}
    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not start checkout: {e}")
    return session["url"]


def _grant_credit_pack(session: dict) -> tuple[bool, str]:
    """Post the purchased grant for a completed pack session. Idempotent.

    Returns `(posted, summary)` rather than the summary alone. The caller
    that counts repairs needs to know whether a row was actually
    written, and reading that out of the text is a trap — "already
    granted" contains "granted".
    """
    from origin.search_engine import credit_ledger  # noqa: PLC0415

    metadata = session.get("metadata") or {}
    pack = metadata.get("genos_credit_pack") or ""
    credits_milli = credit_pack_credits_milli(pack)
    if credits_milli <= 0:
        log.warning("credit pack session with unusable pack %r", pack)
        return False, f"ignored: unknown credit pack {pack!r}"

    # `client_reference_id` first, exactly like the subscription arm:
    # it is set server-side at session creation and the browser never
    # touches it. The customer id is the fallback for a session whose
    # reference is missing.
    user = CustomUser.objects.filter(
        id=session.get("client_reference_id") or None, is_deleted=False
    ).first() or _user_by_customer(session.get("customer"))
    if user is None:
        log.warning("credit pack session %s has no resolvable user", session.get("id"))
        return False, "ignored: unknown user"
    _bind_customer(user, session.get("customer"))

    # Only a PAID session grants. Checkout is card-only so this should
    # never fire, but it is the guard that makes that a fact rather than
    # a hope: if an unpaid session ever completes, credits are withheld
    # until the money actually arrives rather than handed out for a
    # payment that may never settle.
    if session.get("payment_status") not in ("paid", "no_payment_required"):
        log.info("credit pack session %s completed but unpaid; deferring", session.get("id"))
        return False, "pack deferred: awaiting payment"

    posted = credit_ledger.post_purchase(
        user_id=str(user.id),
        credits_milli=credits_milli,
        external_ref=str(session.get("id") or ""),
        reason=f"credit pack {pack}",
    )
    return posted, (
        f"granted {credits_milli // 1000} credits to {user.id}" if posted else "already granted"
    )


def reconcile_credit_packs(user: CustomUser, *, limit: int = 20) -> int:
    """Post any pack grant a lost webhook never delivered. Returns the count.

    There is no self-heal for one-time payments otherwise: the existing
    `reconcile_from_stripe` lists Subscriptions only, so a dropped
    `checkout.session.completed` would leave a customer who paid with
    nothing to show for it and no way to fix it themselves.

    Safe to call on every return from Stripe because `post_purchase` is
    idempotent on the session id — a session already granted posts
    nothing.
    """
    if not user.stripe_customer_id or not billing_enabled():
        return 0
    try:
        stripe = _stripe()
        sessions = stripe.checkout.Session.list(customer=user.stripe_customer_id, limit=limit)
    except Exception:  # noqa: BLE001 — a repair path must not break the caller
        log.warning("could not list checkout sessions for reconcile", exc_info=True)
        return 0

    granted = 0
    for session in json.loads(str(sessions)).get("data", []):
        metadata = session.get("metadata") or {}
        if not metadata.get("genos_credit_pack"):
            continue
        if session.get("status") != "complete":
            continue
        posted, _summary = _grant_credit_pack(session)
        if posted:
            granted += 1
    return granted


# --------------------------------------------------------------------------- #
# Customer portal (+ deep-link flows)                                         #
# --------------------------------------------------------------------------- #
#
# Every plan CHANGE for an existing subscriber goes through the portal,
# never through a second Checkout Session: checkout creates a *new*
# subscription on the same customer, so a "downgrade" bought that way
# leaves two live subscriptions billing in parallel while
# `reconcile_from_stripe` (which takes the BEST active tier) shows
# nothing wrong. The portal's update flow modifies the existing
# subscription in place, with Stripe owning proration, tax and SCA.
#
# `flow_data` turns the portal into a deep link so the plans page can
# offer a per-tier button instead of one generic "manage billing":
#   "update" (+plan) → the confirm-this-exact-switch screen
#   "switch"         → Stripe's own plan picker, for callers that have
#                      no per-tier button to hang "update" off
#   "cancel"         → the cancel screen
# All are still Stripe-hosted and Stripe-validated (it rejects a
# session whose customer doesn't own the subscription).

PORTAL_FLOWS = ("update", "switch", "cancel")

# Non-terminal subscription statuses, in relevance order — the ranking
# used to pick the ONE subscription the overview reports and the portal
# flows target.
_LIVE_STATUS_RANK = {"active": 0, "trialing": 1, "past_due": 2, "paused": 3}


def _best_live_subscription(stripe, customer_id: str) -> dict | None:
    """The customer's most relevant non-terminal subscription, or None.

    Plain dicts, same discipline as `verify_webhook` — see the note in
    `reconcile_from_stripe`.
    """
    try:
        resp = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not list subscriptions: {e}")
    subs = (json.loads(str(resp)) or {}).get("data") or []
    candidates = [s for s in subs if (s or {}).get("status") in _LIVE_STATUS_RANK]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda s: (_LIVE_STATUS_RANK[s.get("status")], -(s.get("created") or 0)),
    )


def _portal_flow_data(stripe, customer_id: str, flow: str, plan: str | None) -> dict | None:
    """`flow_data` for a deep link, or None to use the portal home.

    Returns None rather than raising whenever the deep link can't be
    built (no live subscription, unpriced plan, item-less subscription):
    a user who clicks "Switch to Max" and lands on the portal home can
    still finish the job; one who gets a red error can't. Each None is
    logged, because every one of them means the caller offered a button
    the account can't actually service.
    """
    sub = _best_live_subscription(stripe, customer_id)
    if sub is None:
        log.warning("portal flow %r for customer %s: no live subscription", flow, customer_id)
        return None
    if flow == "cancel":
        # Cancellation MODE (immediate vs at period end) is a property of
        # the portal configuration, not of this call — ours is
        # at_period_end, which is what the UI copy promises.
        return {"type": "subscription_cancel", "subscription_cancel": {"subscription": sub["id"]}}
    if flow == "switch":
        # The picker, not a confirm screen: no price is named, so
        # quantity is whatever Stripe carries over. This is the right
        # flow whenever the caller can't name a target plan.
        return {"type": "subscription_update", "subscription_update": {"subscription": sub["id"]}}
    # An existing subscription CANNOT change currency in Stripe, so the
    # target price has to be in the one they already pay in. Resolving
    # the default currency here would hand a euro subscriber a yen price
    # id and fail the update — or worse, succeed and rebill them in yen.
    existing_currency = (sub.get("currency") or "").lower() or None
    price_id = price_for_plan(plan or "", existing_currency)
    if not price_id:
        log.warning(
            "portal flow 'update' for customer %s: plan %r has no price in %s",
            customer_id,
            plan,
            (existing_currency or default_currency()).upper(),
        )
        return None
    items = (sub.get("items") or {}).get("data") or []
    item = items[0] if items else {}
    if not item.get("id"):
        log.warning("portal flow 'update' for subscription %s: no items", sub.get("id"))
        return None
    return {
        "type": "subscription_update_confirm",
        "subscription_update_confirm": {
            "subscription": sub["id"],
            # `quantity` is carried over EXPLICITLY. It is 1 for a
            # personal subscription but the SEAT COUNT for a team one,
            # and a confirm flow that omits it would resize the team as
            # a side effect of changing plan.
            "items": [{"id": item["id"], "price": price_id, "quantity": item.get("quantity") or 1}],
        },
    }


def _portal_url(stripe, customer_id: str, flow: str | None, plan: str | None, what: str) -> str:
    """Portal session URL, optionally deep-linked into `flow`."""
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    return_url = f"{base}{RETURN_PATH}?billing=portal_return"
    flow_data = _portal_flow_data(stripe, customer_id, flow, plan) if flow else None
    if flow_data:
        # A deep-linked flow otherwise ends on Stripe's own confirmation
        # screen; send the browser back so the return handler runs its
        # reconcile and the new tier lands without waiting on a webhook.
        flow_data["after_completion"] = {
            "type": "redirect",
            "redirect": {"return_url": return_url},
        }
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id, return_url=return_url, flow_data=flow_data
            )
            return session["url"]
        except Exception as e:  # noqa: BLE001
            # Overwhelmingly likely: the portal CONFIGURATION doesn't
            # enable plan updates, or doesn't list this price. That's a
            # dashboard setting, invisible from here and separate from
            # STRIPE_PRICE_* — so degrade to the portal home (where the
            # user can still act) and make the drift loud in the logs.
            log.warning(
                "portal flow %r failed for %s — using portal home instead: %s", flow, what, e
            )
    try:
        session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not open the {what}: {e}")
    return session["url"]


def create_portal_session(
    user: CustomUser, *, flow: str | None = None, plan: str | None = None
) -> str:
    """Customer-portal session URL (plan changes, cancel, invoices).

    `flow="update"` + `plan` deep-links to the switch-to-that-plan
    confirmation; `flow="cancel"` deep-links to the cancel flow; no
    `flow` opens the portal home.
    """
    if not user.stripe_customer_id:
        raise BillingError("No billing account for this user yet.")
    stripe = _stripe()
    return _portal_url(stripe, user.stripe_customer_id, flow, plan, "billing portal")


def verify_webhook(raw_body: bytes, signature_header: str | None) -> dict:
    """Verify a webhook payload; return the event as a PLAIN dict.

    Raises BillingError on any failure — the caller never sees an
    unverified event.

    Why the re-parse: `construct_event` returns a `stripe.Event`
    (`StripeObject`), which is NOT a dict subclass in stripe 5.x+ —
    `event.get(...)` raises `AttributeError`, and the only recursive
    dict conversion the SDK offers is private (`_to_dict_recursive`).
    `construct_event`'s job here is the HMAC check over the raw body;
    once it passes, that same body is safe to `json.loads` into plain
    nested dicts. This keeps `handle_event` on the stdlib dict API
    (and independent of SDK object-model churn).
    """
    webhook_secret = settings.STRIPE.get("WEBHOOK_SECRET") or ""
    if not webhook_secret:
        raise BillingError("Stripe webhook secret is not configured.")
    stripe = _stripe()
    if not signature_header:
        raise BillingError("Missing Stripe-Signature header.")
    try:
        stripe.Webhook.construct_event(raw_body, signature_header, webhook_secret)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Webhook verification failed: {e}")
    try:
        event = json.loads(raw_body.decode("utf-8") or "{}")
    except ValueError as e:
        raise BillingError(f"Webhook body is not valid JSON: {e}")
    if not isinstance(event, dict):
        raise BillingError("Webhook body is not a JSON object.")
    return event


# --------------------------------------------------------------------------- #
# Event handling                                                              #
# --------------------------------------------------------------------------- #


def _user_by_customer(customer_id: str | None) -> CustomUser | None:
    if not customer_id:
        return None
    return CustomUser.objects.filter(stripe_customer_id=customer_id, is_deleted=False).first()


def _subscription_price_id(subscription) -> str | None:
    items = (subscription.get("items") or {}).get("data") or []
    if not items:
        return None
    return ((items[0] or {}).get("price") or {}).get("id")


def handle_event(event) -> str:
    """Apply one verified Stripe event. Returns a short summary string
    (logged + echoed in the 200 body for `stripe listen` ergonomics).
    Unknown event types and unresolvable users are acknowledged and
    ignored — returning non-2xx would only make Stripe retry a payload
    we'll never act on."""
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}

    # A credit pack is the only ONE-OFF purchase in the product, and it
    # is branched on `mode` BEFORE anything else. Falling through would
    # reach the arm that reads `metadata.plan` and sets that subscription
    # tier outright — a pack carrying a stray `plan` key would hand out a
    # paid plan for the price of a top-up. Branching on mode makes that
    # unexpressible rather than merely avoided.
    #
    # Pack checkout is card-only, so a session should never complete
    # unpaid — but `async_payment_succeeded` is still routed, as defense
    # in depth: if the method list is ever widened (deliberately, or by
    # a future Stripe default), delayed settlement grants correctly
    # instead of silently never granting. Both events key on the same
    # session id, so the ledger's uniqueness makes a double-fire a no-op.
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if (obj.get("mode") or "") == "payment":
            return _grant_credit_pack(obj)[1]

    if etype == "checkout.session.completed":
        # Team purchases carry the team id in session metadata (set
        # server-side at session creation, never browser-influenced).
        team_ref = ((obj.get("metadata") or {}).get("genos_team_id") or "").strip()
        if team_ref:
            team = TeamMaster.objects.filter(team_id=team_ref, is_deleted=False).first()
            if team is None:
                log.warning("stripe webhook: %s for unknown team ref %r", etype, team_ref)
                return "ignored: unknown team"
            _bind_team_customer(team, obj.get("customer"))
            plan = ((obj.get("metadata") or {}).get("plan") or "").strip()
            if plan not in PURCHASABLE_PLANS:
                log.warning("stripe webhook: %s with unusable team plan %r", etype, plan)
                return "team customer bound; plan deferred to subscription event"
            _set_team_plan(team, plan, reason=etype)
            return f"team plan set to {plan}"

        user = CustomUser.objects.filter(
            id=obj.get("client_reference_id") or None, is_deleted=False
        ).first()
        if user is None:
            log.warning(
                "stripe webhook: %s for unknown user ref %r", etype, obj.get("client_reference_id")
            )
            return "ignored: unknown user"
        _bind_customer(user, obj.get("customer"))
        plan = ((obj.get("metadata") or {}).get("plan") or "").strip()
        if plan not in PURCHASABLE_PLANS:
            # Metadata missing/garbled — the subscription.updated event
            # that follows will still set the tier from the price id.
            log.warning("stripe webhook: %s with unusable plan %r", etype, plan)
            return "customer bound; tier deferred to subscription event"
        _set_personal_tier(user, plan, reason=etype)
        return f"tier set to {plan}"

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        # Personal and team subscriptions bill DISTINCT Stripe
        # customers; resolve the user first, then the team.
        user = _user_by_customer(obj.get("customer"))
        team = None if user else _team_by_customer(obj.get("customer"))
        if user is None and team is None:
            log.warning("stripe webhook: %s for unknown customer %r", etype, obj.get("customer"))
            return "ignored: unknown customer"
        status_ = obj.get("status") or ""
        if status_ in ("active", "trialing"):
            tier = tier_for_price(_subscription_price_id(obj))
            if tier is None:
                log.warning(
                    "stripe webhook: %s with unmapped price %r — tier NOT changed",
                    etype,
                    _subscription_price_id(obj),
                )
                return "ignored: unmapped price"
            if team is not None:
                _set_team_plan(team, tier, reason=f"{etype}:{status_}")
                return f"team plan set to {tier}"
            _set_personal_tier(user, tier, reason=f"{etype}:{status_}")
            return f"tier set to {tier}"
        if status_ in ("canceled", "unpaid", "incomplete_expired"):
            # Summaries are phrased from what the write RETURNED, here
            # and at every other demotion site: an operator-set tier
            # declines the reset, and a summary that claims otherwise is
            # logged, echoed in the webhook's 200 body, and returned to
            # the browser by `/billing/refresh/`.
            if team is not None:
                if _set_team_plan(team, "free", reason=f"{etype}:{status_}"):
                    return "team plan set to free"
                return f"unchanged: team plan stays {team.plan or 'free'}"
            if _set_personal_tier(user, "free", reason=f"{etype}:{status_}"):
                return "tier set to free"
            return f"unchanged: tier stays {user.tier or 'free'}"
        # past_due / incomplete / paused: keep the current tier —
        # Stripe is still retrying payment (dunning) or checkout never
        # finished; a terminal event will follow either way.
        return f"no-op for status {status_!r}"

    if etype == "customer.subscription.deleted":
        user = _user_by_customer(obj.get("customer"))
        team = None if user else _team_by_customer(obj.get("customer"))
        if user is None and team is None:
            log.warning("stripe webhook: %s for unknown customer %r", etype, obj.get("customer"))
            return "ignored: unknown customer"
        if team is not None:
            if _set_team_plan(team, "free", reason=etype):
                return "team plan set to free"
            return f"unchanged: team plan stays {team.plan or 'free'}"
        if _set_personal_tier(user, "free", reason=etype):
            return "tier set to free"
        return f"unchanged: tier stays {user.tier or 'free'}"

    return f"ignored event type {etype!r}"


# --------------------------------------------------------------------------- #
# Team subscriptions (per-seat)                                               #
# --------------------------------------------------------------------------- #
#
# The Slack model the tier system was designed for: the team OWNER buys
# a quantity-based subscription on the SAME prices as personal plans
# (quantity = active member count), the webhook writes
# `TeamMaster.plan`, and every member inherits through the effective
# tier. Personal and team subscriptions are independent Stripe
# customers; a user may hold both (effective tier takes the best).


def _team_member_ids(team_id) -> list:
    return list(
        TeamMembers.objects.filter(team_id=team_id, is_deleted=False).values_list(
            "attendee_id", flat=True
        )
    )


def team_seats(team_id) -> int:
    """Billable seats = active members. Floor of 1 so a pathological
    empty team can still hold a subscription without a zero-quantity
    error from Stripe."""
    return max(TeamMembers.objects.filter(team_id=team_id, is_deleted=False).count(), 1)


def _set_team_plan(
    team: TeamMaster,
    plan: str,
    *,
    reason: str,
    source: str = TIER_SET_BY_STRIPE,
) -> bool:
    """The same write `feature_access set-team-plan` performs; evicts
    every member's effective-tier cache so the grant lands at once.

    Provenance rules and return value are the personal twin's — see
    `_set_personal_tier`."""
    previous = team.plan or "free"
    previous_set_by = team.plan_set_by or TIER_SET_BY_STRIPE
    if source == TIER_SET_BY_STRIPE and previous_set_by == TIER_SET_BY_OPERATOR and plan == "free":
        log.info(
            "stripe billing: plan for team %s stays %s — operator-set, not Stripe's to remove (%s)",
            team.team_name,
            previous,
            reason,
        )
        return False

    updates: dict[str, str] = {}
    if previous != plan:
        updates["plan"] = plan
    if previous_set_by != source:
        updates["plan_set_by"] = source
    if not updates:
        return False

    for field, value in updates.items():
        setattr(team, field, value)
    team.save(update_fields=list(updates))
    if "plan" in updates:
        invalidate_effective_tier(_team_member_ids(team.team_id))
    log.info(
        "stripe billing: plan for team %s: %s -> %s [source %s -> %s] (%s)",
        team.team_name,
        previous,
        plan,
        previous_set_by,
        source,
        reason,
    )
    return "plan" in updates


def _bind_team_customer(team: TeamMaster, customer_id: str | None) -> None:
    if customer_id and team.stripe_customer_id != customer_id:
        team.stripe_customer_id = customer_id
        team.save(update_fields=["stripe_customer_id"])


def _team_by_customer(customer_id: str | None) -> TeamMaster | None:
    if not customer_id:
        return None
    return TeamMaster.objects.filter(stripe_customer_id=customer_id, is_deleted=False).first()


def ensure_team_customer(team: TeamMaster) -> str:
    """Team twin of `ensure_customer` — same verify-then-replace rule
    for customers deleted in the dashboard."""
    stripe = _stripe()
    if team.stripe_customer_id:
        if _customer_is_alive(stripe, team.stripe_customer_id):
            return team.stripe_customer_id
        log.warning(
            "stripe customer %s for team %s no longer exists — minting a replacement",
            team.stripe_customer_id,
            team.team_name,
        )
    try:
        customer = stripe.Customer.create(
            email=team.team_email,
            name=team.team_name,
            metadata={"genos_team_id": str(team.team_id)},
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not create the team's Stripe customer: {e}")
    _bind_team_customer(team, customer["id"])
    return customer["id"]


def create_team_checkout_session(team: TeamMaster, plan: str, currency: str | None = None) -> str:
    """Quantity-based Checkout Session for a team (owner-gated in the
    view). Same prices as personal plans; quantity = current seats."""
    if plan not in PURCHASABLE_PLANS:
        raise BillingError(f"Unknown plan {plan!r}.")
    price_id = price_for_plan(plan, currency)
    if not price_id:
        raise BillingError(
            f"Plan {plan!r} has no configured Stripe price in "
            f"{(currency or default_currency()).upper()}."
        )
    stripe = _stripe()
    customer_id = ensure_team_customer(team)
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    params = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": team_seats(team.team_id)}],
        # The webhook resolves the TEAM from this metadata — never from
        # anything the browser can influence.
        "metadata": {"genos_team_id": str(team.team_id), "plan": plan},
        "success_url": f"{base}{RETURN_PATH}?billing=success&plan={plan}",
        "cancel_url": f"{base}{RETURN_PATH}?billing=cancelled",
        "automatic_tax": {"enabled": settings.STRIPE.get("AUTOMATIC_TAX", False)},
    }
    if settings.STRIPE.get("TOS_CONSENT"):
        params["consent_collection"] = {"terms_of_service": "required"}
    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not start the team checkout: {e}")
    return session["url"]


def create_team_portal_session(
    team: TeamMaster, *, flow: str | None = None, plan: str | None = None
) -> str:
    """Customer portal for the TEAM's Stripe customer (owner-gated in
    the view): seat/plan changes, cancel, invoices. Same `flow` deep
    links as the personal portal — the update flow preserves the
    subscription's quantity, so switching plan can't resize the team."""
    if not team.stripe_customer_id:
        raise BillingError("No billing account for this team yet.")
    stripe = _stripe()
    return _portal_url(stripe, team.stripe_customer_id, flow, plan, "team billing portal")


def sync_team_subscription_quantity(team_id) -> str:
    """Best-effort seat sync, called from the TeamMembers signal on
    every membership change. Updates the active subscription's quantity
    to the current member count with `create_prorations` — the charge /
    credit folds into the NEXT invoice (no surprise immediate charges).

    Fail-SOFT by design: this runs inside member-management writes, and
    a Stripe hiccup must never block adding or removing a member. Bulk
    membership updates bypass signals (Django semantics) — the next
    signal-triggering change trues the count up.
    """
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None or not team.stripe_customer_id or not billing_enabled():
        return "no-op"
    try:
        stripe = _stripe()
        resp = json.loads(
            str(
                stripe.Subscription.list(
                    customer=team.stripe_customer_id, status="active", limit=10
                )
            )
        )
        subs = resp.get("data") or []
        if not subs:
            return "no active subscription"
        sub = subs[0]
        item = ((sub.get("items") or {}).get("data") or [{}])[0]
        seats = team_seats(team_id)
        if item.get("quantity") == seats:
            return "in sync"
        stripe.Subscription.modify(
            sub["id"],
            items=[{"id": item.get("id"), "quantity": seats}],
            proration_behavior="create_prorations",
        )
        log.info(
            "stripe billing: team %s seats %s -> %s (membership change)",
            team.team_name,
            item.get("quantity"),
            seats,
        )
        return f"quantity set to {seats}"
    except Exception as e:  # noqa: BLE001 — never block member management
        log.warning("stripe seat sync failed for team %s: %s", team_id, e)
        return "failed (logged)"


def reconcile_team_from_stripe(team: TeamMaster) -> str:
    """Team twin of `reconcile_from_stripe` — same write policy, same
    lost-webhook repair. Called from the refresh endpoint for teams the
    refreshing user OWNS."""
    if not team.stripe_customer_id:
        return "no billing account"
    stripe = _stripe()
    try:
        resp = stripe.Subscription.list(customer=team.stripe_customer_id, status="all", limit=100)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not list the team's subscriptions: {e}")
    subs = (json.loads(str(resp)) or {}).get("data") or []
    active_plans: list[str] = []
    unmapped_active = False
    in_grace = False
    for sub in subs:
        status_ = (sub or {}).get("status") or ""
        if status_ in ("active", "trialing"):
            plan = tier_for_price(_subscription_price_id(sub))
            if plan is None:
                unmapped_active = True
            else:
                active_plans.append(plan)
        elif status_ in ("past_due", "incomplete", "paused"):
            in_grace = True
    if active_plans:
        best = max(active_plans, key=PURCHASABLE_PLANS.index)
        _set_team_plan(team, best, reason="reconcile")
        return f"team plan set to {best}"
    if unmapped_active:
        log.warning(
            "stripe reconcile: team %s has an active subscription with an unmapped price"
            " — plan NOT changed",
            team.team_name,
        )
        return "unchanged: active subscription with unmapped price"
    if in_grace:
        return "unchanged: subscription in dunning grace"
    if _set_team_plan(team, "free", reason="reconcile"):
        return "team plan set to free"
    return f"unchanged: team plan stays {team.plan or 'free'}"


# --------------------------------------------------------------------------- #
# Subscription overview (read-only)                                           #
# --------------------------------------------------------------------------- #


def subscription_overview(user: CustomUser) -> dict | None:
    """The user's current subscription, shaped for the Plan & Usage tab.

    Returns None when there is nothing to show (billing disabled, no
    Stripe customer, or no non-terminal subscription) — the UI hides
    the renewal row entirely. Stripe API failures raise BillingError
    (the view maps them to 503).

        {
          "plan": "core" | "pro" | "max" | None,  # None = unmapped price
          "status": "active" | "trialing" | "past_due" | "paused",
          "cancel_at_period_end": bool,
          "current_period_end": <unix ts> | None,
          "cancel_at": <unix ts> | None,
        }
    """
    if not billing_enabled() or not user.stripe_customer_id:
        return None
    best = _best_live_subscription(_stripe(), user.stripe_customer_id)
    if best is None:
        return None
    items = (best.get("items") or {}).get("data") or []
    first_item = items[0] or {} if items else {}
    # Stripe API 2025-03-31 (Basil) moved current_period_end onto the
    # subscription ITEMS; the top-level read is the fallback for
    # accounts pinned to older API versions.
    period_end = first_item.get("current_period_end") or best.get("current_period_end")
    return {
        "plan": tier_for_price(_subscription_price_id(best)),
        "status": best.get("status"),
        "cancel_at_period_end": bool(best.get("cancel_at_period_end")),
        "current_period_end": period_end,
        "cancel_at": best.get("cancel_at"),
    }


# --------------------------------------------------------------------------- #
# Reconciliation (pull)                                                       #
# --------------------------------------------------------------------------- #


def reconcile_from_stripe(user: CustomUser) -> str:
    """Pull the user's subscriptions from Stripe and recompute the tier.

    The webhook projection is push-only and events CAN be lost: locally
    whenever `stripe listen` isn't running (the CLI never retries missed
    events), and in prod when the handler crashes (the webhook view
    deliberately acks 200 to stop retry loops). This is the pull-based
    repair. The frontend calls it whenever the browser lands back with
    `?billing=success` / `?billing=portal_return`, so returning from
    checkout or the portal self-heals regardless of webhook delivery —
    it also beats the redirect-vs-webhook race right after checkout.

    Write policy mirrors `handle_event`:
      * best active/trialing mapped price wins (checkout + portal keep
        one subscription per customer, but "best of active" is the safe
        read if a duplicate ever appears),
      * active subscription with an UNMAPPED price → unchanged (env
        misconfiguration; don't guess),
      * only grace statuses (past_due / incomplete / paused) → unchanged
        (dunning may still recover; a terminal webhook will follow),
      * nothing live at all → free, UNLESS the tier is operator-set
        (`tier_set_by`), in which case the comp is left alone — see
        `_set_personal_tier`,
      * `enterprise` is operator-managed and never touched here.
    """
    if (user.tier or "free") == "enterprise":
        return "skipped: enterprise is operator-managed"
    if not user.stripe_customer_id:
        return "no billing account"
    stripe = _stripe()
    try:
        resp = stripe.Subscription.list(customer=user.stripe_customer_id, status="all", limit=100)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not list subscriptions: {e}")
    # Same plain-dict discipline as `verify_webhook`: `str()` of a
    # StripeObject is its JSON rendering (pinned by test), and plain
    # dicts keep this module off the SDK's object-model churn — the
    # exact churn that broke `.get()` on webhook events once already.
    subs = (json.loads(str(resp)) or {}).get("data") or []

    active_tiers: list[str] = []
    unmapped_active = False
    in_grace = False
    for sub in subs:
        status_ = (sub or {}).get("status") or ""
        if status_ in ("active", "trialing"):
            tier = tier_for_price(_subscription_price_id(sub))
            if tier is None:
                unmapped_active = True
            else:
                active_tiers.append(tier)
        elif status_ in ("past_due", "incomplete", "paused"):
            in_grace = True

    if active_tiers:
        best = max(active_tiers, key=PURCHASABLE_PLANS.index)
        _set_personal_tier(user, best, reason="reconcile")
        return f"tier set to {best}"
    if unmapped_active:
        log.warning(
            "stripe reconcile: %s has an active subscription with an unmapped price"
            " — tier NOT changed",
            user.email,
        )
        return "unchanged: active subscription with unmapped price"
    if in_grace:
        return "unchanged: subscription in dunning grace"
    if _set_personal_tier(user, "free", reason="reconcile"):
        return "tier set to free"
    return f"unchanged: tier stays {user.tier or 'free'}"

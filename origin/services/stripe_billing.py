"""Stripe billing — self-serve personal subscriptions → `CustomUser.tier`.

The tier system (SUBSCRIPTION_TIERS.md) treats `CustomUser.tier` /
`TeamMaster.plan` as the single source of truth; this module is the
Stripe-facing layer that performs exactly the writes the
`feature_access` CLI does, driven by verified webhook events.

Design:
  * Checkout Session (mode=subscription) per plan — `pro` / `max` map
    to the price ids in `settings.STRIPE`. `enterprise` is contact-
    sales and never purchasable here. Team per-seat subscriptions
    (→ `TeamMaster.plan`) are a later phase.
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

All writes are idempotent (set tier + evict the effective-tier cache),
so Stripe's at-least-once delivery needs no dedup table.
"""

from __future__ import annotations

import logging

from django.conf import settings

from origin.models.common.user_models import CustomUser
from origin.search_engine.quota import invalidate_effective_tier

log = logging.getLogger(__name__)

# Plans purchasable self-serve. Deliberately a subset of TIER_CHOICES.
PURCHASABLE_PLANS = ("pro", "max")


class BillingError(Exception):
    """Billing is unconfigured, or a Stripe API call failed. The view
    layer maps this to a clean 4xx/5xx instead of a traceback."""


def _stripe():
    """Return the configured `stripe` module, or raise BillingError.

    Import is lazy (mirrors the tavily pattern in `web_search`) so the
    app boots fine without the package; the failure surfaces only when
    a billing endpoint is actually hit.
    """
    secret = settings.STRIPE.get("SECRET_KEY") or ""
    if not secret:
        raise BillingError("Stripe billing is not configured.")
    try:
        import stripe  # noqa: PLC0415
    except ImportError:
        raise BillingError("The `stripe` package is not installed.")
    stripe.api_key = secret
    return stripe


def billing_enabled() -> bool:
    if not (settings.STRIPE.get("SECRET_KEY") or ""):
        return False
    try:
        import stripe  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def price_for_plan(plan: str) -> str | None:
    return {
        "pro": settings.STRIPE.get("PRICE_PRO") or None,
        "max": settings.STRIPE.get("PRICE_MAX") or None,
    }.get(plan)


def tier_for_price(price_id: str | None) -> str | None:
    """Reverse map: Stripe price id → tier name. None for unknown ids
    (e.g. a price created in the dashboard but not wired into env) —
    callers log-and-ignore rather than guessing."""
    if not price_id:
        return None
    for plan in PURCHASABLE_PLANS:
        if price_for_plan(plan) == price_id:
            return plan
    return None


def purchasable_plans() -> list[str]:
    """Plans with a configured price — what the frontend may offer."""
    return [p for p in PURCHASABLE_PLANS if price_for_plan(p)]


def _set_personal_tier(user: CustomUser, tier: str, *, reason: str) -> None:
    """The same write `feature_access set-tier` performs, attributed."""
    previous = user.tier or "free"
    if previous == tier:
        return
    user.tier = tier
    user.save(update_fields=["tier"])
    invalidate_effective_tier([user.id])
    log.info("stripe billing: tier for %s: %s -> %s (%s)", user.email, previous, tier, reason)


def _bind_customer(user: CustomUser, customer_id: str | None) -> None:
    if customer_id and user.stripe_customer_id != customer_id:
        user.stripe_customer_id = customer_id
        user.save(update_fields=["stripe_customer_id"])


def ensure_customer(user: CustomUser) -> str:
    """Reuse the stored Stripe customer, or create one. The customer
    carries our user id in metadata for dashboard-side debugging; the
    authoritative link is the `stripe_customer_id` column."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    stripe = _stripe()
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


def create_checkout_session(user: CustomUser, plan: str) -> str:
    """Create a subscription Checkout Session; return its redirect URL."""
    if plan not in PURCHASABLE_PLANS:
        raise BillingError(f"Unknown plan {plan!r}.")
    price_id = price_for_plan(plan)
    if not price_id:
        raise BillingError(f"Plan {plan!r} has no configured Stripe price.")
    stripe = _stripe()
    customer_id = ensure_customer(user)
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            # The webhook resolves the user from this — never from
            # anything the browser can influence.
            client_reference_id=str(user.id),
            metadata={"genos_user_id": str(user.id), "plan": plan},
            success_url=f"{base}/?billing=success&plan={plan}",
            cancel_url=f"{base}/?billing=cancelled",
            # Stripe Tax: activates only when enabled on the account
            # (dashboard: Settings → Tax). Harmless flag otherwise per
            # Stripe docs; if account setup is incomplete Stripe returns
            # a clear error at session-create time, surfaced as a 503.
            automatic_tax={"enabled": settings.STRIPE.get("AUTOMATIC_TAX", False)},
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not start checkout: {e}")
    return session["url"]


def create_portal_session(user: CustomUser) -> str:
    """Customer-portal session URL (plan changes, cancel, invoices)."""
    if not user.stripe_customer_id:
        raise BillingError("No billing account for this user yet.")
    stripe = _stripe()
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{base}/?billing=portal_return",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not open the billing portal: {e}")
    return session["url"]


def verify_webhook(raw_body: bytes, signature_header: str | None):
    """Verify + parse a webhook payload. Raises BillingError on any
    failure — the caller never sees an unverified event object."""
    webhook_secret = settings.STRIPE.get("WEBHOOK_SECRET") or ""
    if not webhook_secret:
        raise BillingError("Stripe webhook secret is not configured.")
    stripe = _stripe()
    if not signature_header:
        raise BillingError("Missing Stripe-Signature header.")
    try:
        return stripe.Webhook.construct_event(raw_body, signature_header, webhook_secret)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Webhook verification failed: {e}")


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

    if etype == "checkout.session.completed":
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
        user = _user_by_customer(obj.get("customer"))
        if user is None:
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
            _set_personal_tier(user, tier, reason=f"{etype}:{status_}")
            return f"tier set to {tier}"
        if status_ in ("canceled", "unpaid", "incomplete_expired"):
            _set_personal_tier(user, "free", reason=f"{etype}:{status_}")
            return "tier set to free"
        # past_due / incomplete / paused: keep the current tier —
        # Stripe is still retrying payment (dunning) or checkout never
        # finished; a terminal event will follow either way.
        return f"no-op for status {status_!r}"

    if etype == "customer.subscription.deleted":
        user = _user_by_customer(obj.get("customer"))
        if user is None:
            log.warning("stripe webhook: %s for unknown customer %r", etype, obj.get("customer"))
            return "ignored: unknown customer"
        _set_personal_tier(user, "free", reason=etype)
        return "tier set to free"

    return f"ignored event type {etype!r}"

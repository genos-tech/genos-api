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

import json
import logging

from django.conf import settings
from django.core.cache import cache

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.quota import invalidate_effective_tier

log = logging.getLogger(__name__)

# Plans purchasable self-serve. Deliberately a subset of TIER_CHOICES.
PURCHASABLE_PLANS = ("pro", "max")

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


def price_display(plan: str) -> dict | None:
    """`{"amount", "currency", "interval"}` for a purchasable plan,
    read from its Stripe price so the page can never advertise an
    amount Stripe won't charge. Cached for an hour, and fail-SOFT:
    billing disabled, unmapped plan, or a Stripe error all return None
    — the plans page then renders limits without a price line rather
    than failing. `amount` is in the currency's smallest unit as Stripe
    stores it (JPY is zero-decimal: 1200 == ¥1,200)."""
    price_id = price_for_plan(plan)
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


def create_portal_session(user: CustomUser) -> str:
    """Customer-portal session URL (plan changes, cancel, invoices)."""
    if not user.stripe_customer_id:
        raise BillingError("No billing account for this user yet.")
    stripe = _stripe()
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    try:
        session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{base}{RETURN_PATH}?billing=portal_return",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not open the billing portal: {e}")
    return session["url"]


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
            if team is not None:
                _set_team_plan(team, "free", reason=f"{etype}:{status_}")
                return "team plan set to free"
            _set_personal_tier(user, "free", reason=f"{etype}:{status_}")
            return "tier set to free"
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
            _set_team_plan(team, "free", reason=etype)
            return "team plan set to free"
        _set_personal_tier(user, "free", reason=etype)
        return "tier set to free"

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


def _set_team_plan(team: TeamMaster, plan: str, *, reason: str) -> None:
    """The same write `feature_access set-team-plan` performs; evicts
    every member's effective-tier cache so the grant lands at once."""
    previous = team.plan or "free"
    if previous == plan:
        return
    team.plan = plan
    team.save(update_fields=["plan"])
    invalidate_effective_tier(_team_member_ids(team.team_id))
    log.info(
        "stripe billing: plan for team %s: %s -> %s (%s)",
        team.team_name,
        previous,
        plan,
        reason,
    )


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


def create_team_checkout_session(team: TeamMaster, plan: str) -> str:
    """Quantity-based Checkout Session for a team (owner-gated in the
    view). Same prices as personal plans; quantity = current seats."""
    if plan not in PURCHASABLE_PLANS:
        raise BillingError(f"Unknown plan {plan!r}.")
    price_id = price_for_plan(plan)
    if not price_id:
        raise BillingError(f"Plan {plan!r} has no configured Stripe price.")
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


def create_team_portal_session(team: TeamMaster) -> str:
    """Customer portal for the TEAM's Stripe customer (owner-gated in
    the view): seat/plan changes, cancel, invoices."""
    if not team.stripe_customer_id:
        raise BillingError("No billing account for this team yet.")
    stripe = _stripe()
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    try:
        session = stripe.billing_portal.Session.create(
            customer=team.stripe_customer_id,
            return_url=f"{base}{RETURN_PATH}?billing=portal_return",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not open the team billing portal: {e}")
    return session["url"]


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
            str(stripe.Subscription.list(customer=team.stripe_customer_id, status="active", limit=10))
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
    _set_team_plan(team, "free", reason="reconcile")
    return "team plan set to free"


# --------------------------------------------------------------------------- #
# Subscription overview (read-only)                                           #
# --------------------------------------------------------------------------- #

# Non-terminal statuses the overview reports, in relevance order.
_OVERVIEW_STATUS_RANK = {"active": 0, "trialing": 1, "past_due": 2, "paused": 3}


def subscription_overview(user: CustomUser) -> dict | None:
    """The user's current subscription, shaped for the Plan & Usage tab.

    Returns None when there is nothing to show (billing disabled, no
    Stripe customer, or no non-terminal subscription) — the UI hides
    the renewal row entirely. Stripe API failures raise BillingError
    (the view maps them to 503).

        {
          "plan": "pro" | "max" | None,       # None = unmapped price
          "status": "active" | "trialing" | "past_due" | "paused",
          "cancel_at_period_end": bool,
          "current_period_end": <unix ts> | None,
          "cancel_at": <unix ts> | None,
        }
    """
    if not billing_enabled() or not user.stripe_customer_id:
        return None
    stripe = _stripe()
    try:
        resp = stripe.Subscription.list(customer=user.stripe_customer_id, status="all", limit=100)
    except Exception as e:  # noqa: BLE001
        raise BillingError(f"Could not list subscriptions: {e}")
    subs = (json.loads(str(resp)) or {}).get("data") or []
    candidates = [s for s in subs if (s or {}).get("status") in _OVERVIEW_STATUS_RANK]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda s: (_OVERVIEW_STATUS_RANK[s.get("status")], -(s.get("created") or 0)),
    )
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
      * nothing live at all → free,
      * `enterprise` is operator-managed and never touched here.
    """
    if (user.tier or "free") == "enterprise":
        return "skipped: enterprise is operator-managed"
    if not user.stripe_customer_id:
        return "no billing account"
    stripe = _stripe()
    try:
        resp = stripe.Subscription.list(
            customer=user.stripe_customer_id, status="all", limit=100
        )
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
    _set_personal_tier(user, "free", reason="reconcile")
    return "tier set to free"

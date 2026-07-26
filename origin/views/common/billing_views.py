"""Billing endpoints — Stripe checkout / portal / webhook.

Thin HTTP layer over `origin.services.stripe_billing`; every tier
decision lives in the service. The webhook view copies the
`GithubWebhookView` pattern (csrf-exempt, unauthenticated, verifies a
signature over the RAW body before touching the payload).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.team_models import TeamMaster
from origin.search_engine import credit_ledger
from origin.services import stripe_billing
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)


class BillingConfigView(AuthenticatedAPIView):
    """GET /api/v2/billing/config/

    What the Plan & Usage tab needs to decide which billing buttons to
    render:
        {
          "enabled": bool,           # Stripe configured server-side
          "plans": ["pro", "max"],   # purchasable (price id configured)
          "personal_tier": "free",   # the user's OWN tier (a team plan
                                      # may still lift their effective tier)
          "has_billing_account": bool  # Stripe customer exists → portal works
        }
    """

    def get(self, request):
        return Response(
            {
                "enabled": stripe_billing.billing_enabled(),
                "plans": stripe_billing.purchasable_plans(_requested_currency(request)),
                "currency": _requested_currency(request),
                "supported_currencies": stripe_billing.supported_currencies(),
                "personal_tier": request.user.tier or "free",
                "has_billing_account": bool(request.user.stripe_customer_id),
            }
        )


def _requested_currency(request) -> str:
    """The currency this request should be priced in.

    Explicit `currency` wins (the pricing page passes what the visitor
    picked); otherwise the configured default. Anything we have no
    prices for falls back to the default rather than 404ing — a bad
    query string should show a pricing page, not break one.

    Deliberately NOT geo-inferred. Guessing from an IP shows a
    travelling customer the wrong currency and, worse, quietly changes
    what they are about to be billed in.
    """
    raw = ""
    if request.method == "GET":
        raw = (request.query_params.get("currency") or "").strip().lower()
    else:
        raw = str((request.data or {}).get("currency") or "").strip().lower()
    supported = stripe_billing.supported_currencies()
    return raw if raw in supported else stripe_billing.default_currency()


class BillingCheckoutView(AuthenticatedAPIView):
    """POST /api/v2/billing/checkout/   body: {"plan": "pro" | "max"}

    Returns `{"url": ...}` — the frontend redirects the browser there.
    The tier is NOT changed here; only the verified webhook does that.
    """

    def post(self, request):
        plan = (request.data or {}).get("plan") or ""
        if plan not in stripe_billing.PURCHASABLE_PLANS:
            return Response(
                {"error": f"Unknown plan {plan!r}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            url = stripe_billing.create_checkout_session(
                request.user, plan, _requested_currency(request)
            )
        except stripe_billing.BillingError as e:
            logger.warning("billing checkout failed for %s: %s", request.user.email, e)
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


def _portal_flow_or_error(data):
    """Validate the optional `flow` / `plan` deep-link params.

    Returns `((flow, plan), None)` or `(None, Response)`. An absent flow
    is valid and means the portal home.
    """
    flow = ((data or {}).get("flow") or "").strip() or None
    if flow is not None and flow not in stripe_billing.PORTAL_FLOWS:
        return None, Response(
            {"error": f"Unknown portal flow {flow!r}."}, status=status.HTTP_400_BAD_REQUEST
        )
    plan = ((data or {}).get("plan") or "").strip() or None
    if flow == "update" and plan not in stripe_billing.PURCHASABLE_PLANS:
        return None, Response(
            {"error": f"Unknown plan {plan!r}."}, status=status.HTTP_400_BAD_REQUEST
        )
    return (flow, plan), None


class BillingPortalView(AuthenticatedAPIView):
    """POST /api/v2/billing/portal/ → {"url": ...}

    Body (all optional):
        {"flow": "update", "plan": "max"}  → confirm-the-switch screen
        {"flow": "cancel"}                 → cancel screen
        {}                                 → portal home

    Stripe customer portal: plan changes, cancellation, payment method,
    invoice history. Available once a customer exists. The deep-link
    flows are what the plans page's per-tier buttons use — an existing
    subscriber must never be sent back through Checkout, which would
    open a SECOND parallel subscription rather than change the one they
    have.
    """

    def post(self, request):
        parsed, err = _portal_flow_or_error(request.data)
        if err:
            return err
        flow, plan = parsed
        try:
            url = stripe_billing.create_portal_session(request.user, flow=flow, plan=plan)
        except stripe_billing.BillingError as e:
            logger.warning("billing portal failed for %s: %s", request.user.email, e)
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


# The dimensions the plans page compares. Deliberately excludes
# `model_daily` — per-model caps churn with the model catalog; the page
# carries a static "per-model caps apply" footnote instead.
_PLAN_LIMIT_KEYS = (
    "llm_ask_daily",
    "web_search_daily",
    "task_create_monthly",
    "note_create_monthly",
    "message_retention_days",
    "upload_max_mb",
)


class BillingPlansView(AuthenticatedAPIView):
    """GET /api/v2/billing/plans/ — the tier comparison the plans page renders.

    Limits come straight from `SEARCH_ENGINE["TIER_QUOTAS"]` — the
    enforcement table — so the page can never advertise a limit the
    quota engine doesn't apply. Prices come from Stripe (cached,
    fail-soft null). Enterprise is contact-sales: no price, never
    purchasable.

    Public (AllowAny): the marketing pricing page renders this while
    logged out. Safe because the response is user-agnostic — tier
    limits and public Stripe prices only, no request.user access, and
    the Stripe price lookup is process-cached (1h) so it can't be
    flooded into a per-request Stripe call.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def get(self, request):
        quotas = settings.SEARCH_ENGINE.get("TIER_QUOTAS") or {}
        currency = _requested_currency(request)
        purchasable = stripe_billing.purchasable_plans(currency)
        credits_on = credit_ledger.credits_authoritative()
        tiers = []
        for tier in ("free", "core", "pro", "max", "enterprise"):
            cfg = quotas.get(tier)
            if cfg is None:
                continue
            if tier == "free":
                # Free is free in every currency, but the CODE still has
                # to be right: the client formats with Intl and would
                # render "¥0" to a dollar visitor otherwise.
                price = {"amount": 0, "currency": currency, "interval": "month"}
            elif tier in stripe_billing.PURCHASABLE_PLANS:
                price = stripe_billing.price_display(tier, currency)
            else:
                price = None
            limits = {k: cfg.get(k) for k in _PLAN_LIMIT_KEYS}
            # The plan's monthly AI credits, present ONLY when credits
            # are the limit the server actually enforces. Its presence is
            # the client's render switch — the same payload-shape
            # convention `credits` uses on /agent/features/ — so the
            # comparison table can never advertise a limit the quota
            # engine has stopped applying, in either direction, without
            # a coordinated deploy.
            if credits_on:
                ent = credit_ledger.entitlement_milli(tier)
                # None = unlimited (enterprise); milli -> whole credits.
                limits["monthly_ai_credits"] = None if ent is None else ent // 1000
            tiers.append(
                {
                    "tier": tier,
                    "price": price,
                    "purchasable": tier in purchasable,
                    "contact_sales": tier == "enterprise",
                    "limits": limits,
                }
            )
        return Response(
            {
                "billing_enabled": stripe_billing.billing_enabled(),
                "currency": currency,
                "supported_currencies": stripe_billing.supported_currencies(),
                "tiers": tiers,
            }
        )


class BillingSubscriptionView(AuthenticatedAPIView):
    """GET /api/v2/billing/subscription/ → {"subscription": {...} | null}

    Renewal/expiry state for the Plan & Usage tab: which plan the
    user's live subscription is on, when the current period ends, and
    whether a cancellation is scheduled. Null when there is nothing to
    show (no billing account, billing disabled, no live subscription).
    """

    def get(self, request):
        try:
            overview = stripe_billing.subscription_overview(request.user)
        except stripe_billing.BillingError as e:
            logger.warning("billing subscription lookup failed for %s: %s", request.user.email, e)
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"subscription": overview})


class BillingRefreshView(AuthenticatedAPIView):
    """POST /api/v2/billing/refresh/ → {"detail": ..., "personal_tier": ...}

    Pull-based reconcile: re-reads the user's subscriptions from Stripe
    and rewrites the tier (`stripe_billing.reconcile_from_stripe`),
    plus the plan of every team the user OWNS with a billing account.
    The frontend fires this when the browser returns from checkout or
    the customer portal, so tiers are correct even when the webhook was
    lost or simply hasn't arrived yet.
    """

    def post(self, request):
        try:
            summary = stripe_billing.reconcile_from_stripe(request.user)
            for team in TeamMaster.objects.filter(
                owner=request.user, is_deleted=False
            ).exclude(stripe_customer_id__isnull=True).exclude(stripe_customer_id=""):
                stripe_billing.reconcile_team_from_stripe(team)
        except stripe_billing.BillingError as e:
            logger.warning("billing refresh failed for %s: %s", request.user.email, e)
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        logger.info("billing refresh for %s: %s", request.user.email, summary)
        return Response({"detail": summary, "personal_tier": request.user.tier or "free"})


def _owned_team_or_error(request, team_id):
    """Resolve a team the requester OWNS, or the error Response.

    Non-owners get the same 404 as a nonexistent team — membership in
    someone else's billing is not a thing to confirm or deny.
    """
    if not team_id:
        return None, Response(
            {"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST
        )
    team = TeamMaster.objects.filter(
        team_id=team_id, owner=request.user, is_deleted=False
    ).first()
    if team is None:
        return None, Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    return team, None


class TeamBillingConfigView(AuthenticatedAPIView):
    """GET /api/v2/billing/team/config/ → {"teams": [...]}

    The teams the requester OWNS, shaped for the team-billing UI:
        [{"team_id", "team_name", "plan", "seats", "has_billing_account"}]
    Empty list for non-owners — the UI simply shows no team section.
    """

    def get(self, request):
        teams = [
            {
                "team_id": str(t.team_id),
                "team_name": t.team_name,
                "plan": t.plan or "free",
                "seats": stripe_billing.team_seats(t.team_id),
                "has_billing_account": bool(t.stripe_customer_id),
            }
            for t in TeamMaster.objects.filter(owner=request.user, is_deleted=False)
        ]
        return Response({"enabled": stripe_billing.billing_enabled(), "teams": teams})


class TeamBillingCheckoutView(AuthenticatedAPIView):
    """POST /api/v2/billing/team/checkout/  {"team_id", "plan"} → {"url"}

    Owner-only (one payer). Quantity = the team's current seats; the
    seat-sync signal keeps it true afterwards.
    """

    def post(self, request):
        plan = (request.data or {}).get("plan") or ""
        if plan not in stripe_billing.PURCHASABLE_PLANS:
            return Response(
                {"error": f"Unknown plan {plan!r}."}, status=status.HTTP_400_BAD_REQUEST
            )
        team, err = _owned_team_or_error(request, (request.data or {}).get("team_id"))
        if err:
            return err
        try:
            url = stripe_billing.create_team_checkout_session(
                team, plan, _requested_currency(request)
            )
        except stripe_billing.BillingError as e:
            logger.warning(
                "team billing checkout failed for %s / %s: %s",
                request.user.email,
                team.team_name,
                e,
            )
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


class TeamBillingPortalView(AuthenticatedAPIView):
    """POST /api/v2/billing/team/portal/ {"team_id", "flow"?, "plan"?} → {"url"}

    Owner-only. Same optional deep-link flows as the personal portal.
    """

    def post(self, request):
        parsed, err = _portal_flow_or_error(request.data)
        if err:
            return err
        flow, plan = parsed
        team, err = _owned_team_or_error(request, (request.data or {}).get("team_id"))
        if err:
            return err
        try:
            url = stripe_billing.create_team_portal_session(team, flow=flow, plan=plan)
        except stripe_billing.BillingError as e:
            logger.warning(
                "team billing portal failed for %s / %s: %s",
                request.user.email,
                team.team_name,
                e,
            )
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(APIView):
    """POST /api/v2/billing/stripe/webhook/

    Stripe's server calls this — no JWT, no CSRF; authenticity comes
    from the `Stripe-Signature` verification over the raw body.
    Signature failures get 400 (Stripe surfaces these in the dashboard);
    verified events are always acknowledged 200, including ones we
    ignore — retries would never change the outcome.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request):
        try:
            event = stripe_billing.verify_webhook(
                request.body,
                request.headers.get("Stripe-Signature")
                or request.META.get("HTTP_STRIPE_SIGNATURE"),
            )
        except stripe_billing.BillingError as e:
            logger.warning("stripe webhook rejected: %s", e)
            return Response({"detail": "invalid_signature"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            summary = stripe_billing.handle_event(event)
        except Exception:  # noqa: BLE001 — ack anyway, but loudly
            # A handler bug must not make Stripe retry forever against
            # the same crash; log with the event id for replay via the
            # dashboard once fixed. The id lookup is deliberately
            # defensive: this ran while already handling a failure, and
            # an id extraction that itself raises would mask the real
            # traceback (it did exactly that once — `.get` on a
            # StripeObject).
            event_id = event.get("id") if isinstance(event, dict) else None
            logger.exception("stripe webhook handler crashed for event %s", event_id)
            return Response({"detail": "handler_error"}, status=status.HTTP_200_OK)

        logger.info("stripe webhook %s (%s): %s", event.get("id"), event.get("type"), summary)
        return Response({"detail": summary}, status=status.HTTP_200_OK)

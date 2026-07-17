"""Billing endpoints — Stripe checkout / portal / webhook.

Thin HTTP layer over `origin.services.stripe_billing`; every tier
decision lives in the service. The webhook view copies the
`GithubWebhookView` pattern (csrf-exempt, unauthenticated, verifies a
signature over the RAW body before touching the payload).
"""

from __future__ import annotations

import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

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
                "plans": stripe_billing.purchasable_plans(),
                "personal_tier": request.user.tier or "free",
                "has_billing_account": bool(request.user.stripe_customer_id),
            }
        )


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
            url = stripe_billing.create_checkout_session(request.user, plan)
        except stripe_billing.BillingError as e:
            logger.warning("billing checkout failed for %s: %s", request.user.email, e)
            return Response({"error": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"url": url})


class BillingPortalView(AuthenticatedAPIView):
    """POST /api/v2/billing/portal/ → {"url": ...}

    Stripe customer portal: plan changes, cancellation, payment
    method, invoice history. Available once a customer exists.
    """

    def post(self, request):
        try:
            url = stripe_billing.create_portal_session(request.user)
        except stripe_billing.BillingError as e:
            logger.warning("billing portal failed for %s: %s", request.user.email, e)
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

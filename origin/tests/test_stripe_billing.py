"""Tests for the Stripe billing layer (service + endpoints + webhook).

Two layers, deliberately:

  * Most tests mock at the service seam (no network, no real keys) and
    feed `handle_event` plain dicts — fast coverage of the tier-write
    matrix.
  * `VerifyWebhookRealSdkTests` runs the REAL
    `stripe.Webhook.construct_event` over a genuinely HMAC-signed body.
    This layer exists because the mocked layer alone shipped a 500:
    the mocks asserted the dict shape we *assumed*, while the SDK
    actually returns a non-dict `StripeObject`. Any test that mocks
    `verify_webhook` is asserting our own assumption — the real-SDK
    class is what pins the contract with Stripe.

Tier writes are asserted against the DB, including the effective-tier
cache eviction.
"""

import hashlib
import hmac
import json
import time
from unittest import mock

from django.test import override_settings

from origin.models.common.user_models import TIER_SOURCE_OPERATOR, TIER_SOURCE_STRIPE
from origin.search_engine import quota
from origin.services import stripe_billing

from .test_base import BaseAPITestCase

CONFIG_URL = "/api/v2/billing/config/"
CHECKOUT_URL = "/api/v2/billing/checkout/"
PORTAL_URL = "/api/v2/billing/portal/"
PLANS_URL = "/api/v2/billing/plans/"
REFRESH_URL = "/api/v2/billing/refresh/"
SUBSCRIPTION_URL = "/api/v2/billing/subscription/"
WEBHOOK_URL = "/api/v2/billing/stripe/webhook/"

STRIPE_TEST_SETTINGS = {
    "SECRET_KEY": "sk_test_x",
    "WEBHOOK_SECRET": "whsec_x",
    "PRICE_CORE": "price_core_789",
    "PRICE_PRO": "price_pro_123",
    "PRICE_MAX": "price_max_456",
    # Pre-repricing ids: pro used to be JPY1,200 and max JPY2,500.
    # Subscriptions bought at those prices keep renewing on them.
    "PRICE_PRO_LEGACY": "price_pro_old_1200",
    "PRICE_MAX_LEGACY": "price_max_old_2500",
    "AUTOMATIC_TAX": False,
    "DEFAULT_CURRENCY": "jpy",
    "PRICES_BY_CURRENCY": {},
}

STRIPE_DISABLED_SETTINGS = {
    "SECRET_KEY": "",
    "WEBHOOK_SECRET": "",
    "PRICE_CORE": "",
    "PRICE_PRO": "",
    "PRICE_MAX": "",
    "PRICE_PRO_LEGACY": "",
    "PRICE_MAX_LEGACY": "",
    "AUTOMATIC_TAX": False,
    "DEFAULT_CURRENCY": "jpy",
    "PRICES_BY_CURRENCY": {},
}


class BillingTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()

    def checkout_completed_event(self, *, user=None, plan="pro", customer="cus_abc"):
        return {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": str((user or self.user).id),
                    "customer": customer,
                    "metadata": {"plan": plan},
                }
            },
        }

    def subscription_event(self, *, etype, status_, price="price_pro_123", customer="cus_abc"):
        return {
            "id": "evt_2",
            "type": etype,
            "data": {
                "object": {
                    "customer": customer,
                    "status": status_,
                    "items": {"data": [{"price": {"id": price}}]},
                }
            },
        }


@override_settings(STRIPE=STRIPE_DISABLED_SETTINGS)
class BillingDisabledTests(BillingTestBase):
    def test_config_reports_disabled(self):
        res = self.client.get(CONFIG_URL)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["enabled"])
        self.assertEqual(res.data["plans"], [])
        self.assertEqual(res.data["personal_tier"], "free")
        self.assertFalse(res.data["has_billing_account"])

    def test_checkout_503_when_disabled(self):
        res = self.client.post(CHECKOUT_URL, {"plan": "pro"}, format="json")
        self.assertEqual(res.status_code, 503)

    def test_webhook_400_without_secret(self):
        res = self.client.post(WEBHOOK_URL, data=b"{}", content_type="application/json")
        self.assertEqual(res.status_code, 400)


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class BillingConfigTests(BillingTestBase):
    def test_config_enabled_with_plans(self):
        res = self.client.get(CONFIG_URL)
        self.assertTrue(res.data["enabled"])
        self.assertEqual(res.data["plans"], ["core", "pro", "max"])

    def test_enterprise_never_purchasable(self):
        self.assertNotIn("enterprise", stripe_billing.PURCHASABLE_PLANS)

    def test_partial_price_config_limits_plans(self):
        with override_settings(STRIPE={**STRIPE_TEST_SETTINGS, "PRICE_MAX": ""}):
            res = self.client.get(CONFIG_URL)
            self.assertEqual(res.data["plans"], ["core", "pro"])

    def test_legacy_price_resolves_to_grandfathered_plan(self):
        """A subscription bought before the repricing must keep its plan.

        Stripe never repoints an existing subscription at a new price
        object, so renewals arrive carrying the ORIGINAL price id. If
        those stopped resolving, every grandfathered subscriber would
        silently decay to free on their next renewal event.
        """
        self.assertEqual(stripe_billing.tier_for_price("price_pro_old_1200"), "pro")
        self.assertEqual(stripe_billing.tier_for_price("price_max_old_2500"), "max")

    def test_legacy_prices_are_not_purchasable(self):
        """Grandfathered ids resolve on webhooks but are never sold."""
        self.assertEqual(stripe_billing.price_for_plan("pro"), "price_pro_123")
        self.assertEqual(stripe_billing.price_for_plan("max"), "price_max_456")
        self.assertEqual(stripe_billing.price_for_plan("core"), "price_core_789")

    def test_legacy_pro_price_does_not_become_core(self):
        """The old pro price is numerically today's CORE price (JPY1,200).

        It must still grant `pro` — grandfathered subscribers keep the
        plan they bought, not the plan that now costs what they pay.
        """
        self.assertNotEqual(stripe_billing.tier_for_price("price_pro_old_1200"), "core")

    def test_unmapped_price_still_returns_none(self):
        self.assertIsNone(stripe_billing.tier_for_price("price_never_seen"))
        self.assertIsNone(stripe_billing.tier_for_price(None))

    def test_has_billing_account_reflects_customer_id(self):
        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])
        res = self.client.get(CONFIG_URL)
        self.assertTrue(res.data["has_billing_account"])


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class CheckoutAndPortalViewTests(BillingTestBase):
    def test_invalid_plan_400(self):
        res = self.client.post(CHECKOUT_URL, {"plan": "enterprise"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_checkout_returns_redirect_url(self):
        with mock.patch.object(
            stripe_billing, "create_checkout_session", return_value="https://stripe/cs_1"
        ) as create:
            res = self.client.post(CHECKOUT_URL, {"plan": "max"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["url"], "https://stripe/cs_1")
        create.assert_called_once()
        self.assertEqual(create.call_args.args[1], "max")

    def test_checkout_billing_error_maps_to_503(self):
        with mock.patch.object(
            stripe_billing,
            "create_checkout_session",
            side_effect=stripe_billing.BillingError("boom"),
        ):
            res = self.client.post(CHECKOUT_URL, {"plan": "pro"}, format="json")
        self.assertEqual(res.status_code, 503)

    def test_portal_without_customer_503(self):
        res = self.client.post(PORTAL_URL, {}, format="json")
        self.assertEqual(res.status_code, 503)

    def _create_session_kwargs(self, stripe_settings):
        """Run create_checkout_session against REAL-SDK-shaped mocks
        and return the kwargs Stripe would have received."""
        import stripe  # noqa: PLC0415

        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])
        session = stripe.checkout.Session.construct_from(
            {"id": "cs_x", "object": "checkout.session", "url": "https://stripe/cs_x"},
            "sk_test_x",
        )
        # ensure_customer verifies the stored customer against Stripe.
        alive = stripe.Customer.construct_from({"id": "cus_abc", "object": "customer"}, "sk_test_x")
        with (
            override_settings(STRIPE=stripe_settings),
            mock.patch("stripe.Customer.retrieve", return_value=alive),
            mock.patch("stripe.checkout.Session.create", return_value=session) as create,
        ):
            url = stripe_billing.create_checkout_session(self.user, "pro")
        self.assertEqual(url, "https://stripe/cs_x")
        return create.call_args.kwargs

    def test_checkout_tos_consent_off_by_default(self):
        kwargs = self._create_session_kwargs(STRIPE_TEST_SETTINGS)
        self.assertNotIn("consent_collection", kwargs)

    def test_return_urls_land_inside_the_workspace(self):
        """Regression: these pointed at the app ROOT, which is the
        guest-only sign-in route — GuestGuard bounced signed-in users
        to /jointeam and dropped the ?billing= param, so the return
        toast and the tier reconcile never ran in a real browser."""
        kwargs = self._create_session_kwargs(STRIPE_TEST_SETTINGS)
        for key in ("success_url", "cancel_url"):
            self.assertIn(
                stripe_billing.RETURN_PATH + "?billing=", kwargs[key], f"{key} must land in-app"
            )
        self.assertTrue(stripe_billing.RETURN_PATH.startswith("/workspace/"))

    def test_checkout_tos_consent_flag_adds_required_checkbox(self):
        kwargs = self._create_session_kwargs({**STRIPE_TEST_SETTINGS, "TOS_CONSENT": True})
        self.assertEqual(kwargs["consent_collection"], {"terms_of_service": "required"})
        # The flag must not disturb the load-bearing params.
        self.assertEqual(kwargs["client_reference_id"], str(self.user.id))
        self.assertEqual(kwargs["mode"], "subscription")

    def test_portal_returns_url(self):
        with mock.patch.object(
            stripe_billing, "create_portal_session", return_value="https://stripe/bps_1"
        ):
            res = self.client.post(PORTAL_URL, {}, format="json")
        self.assertEqual(res.data["url"], "https://stripe/bps_1")


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class PortalFlowTests(BillingTestBase):
    """Deep-linked portal flows — what the plans page's per-tier
    upgrade / switch / cancel buttons call.

    The load-bearing property is that an EXISTING subscriber's plan
    change goes through `subscription_update_confirm` on their current
    subscription. Routing it through Checkout instead would open a
    second parallel subscription on the same customer, and
    `reconcile_from_stripe` (best active tier wins) would show nothing
    amiss while the user paid twice.
    """

    def setUp(self):
        super().setUp()
        self.user.tier = "core"
        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["tier", "stripe_customer_id"])

    @staticmethod
    def _list_mock(*subs):
        import stripe  # noqa: PLC0415 — lazy like the service itself

        payload = {
            "object": "list",
            "data": list(subs),
            "has_more": False,
            "url": "/v1/subscriptions",
        }
        return mock.patch(
            "stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(payload, "sk_test_x"),
        )

    @staticmethod
    def _sub(status_="active", price="price_core_789", quantity=1):
        return {
            "id": "sub_x",
            "object": "subscription",
            "status": status_,
            "created": 1,
            "items": {"data": [{"id": "si_x", "price": {"id": price}, "quantity": quantity}]},
        }

    @staticmethod
    def _session_mock():
        import stripe  # noqa: PLC0415

        return mock.patch(
            "stripe.billing_portal.Session.create",
            return_value=stripe.billing_portal.Session.construct_from(
                {"id": "bps_x", "object": "billing_portal.session", "url": "https://stripe/bps_x"},
                "sk_test_x",
            ),
        )

    def _portal_kwargs(self, *, flow=None, plan=None, subs=(None,), create=None):
        """Run create_portal_session and return the kwargs Stripe got."""
        subs = tuple(s for s in subs if s is not None)
        with self._list_mock(*subs), create or self._session_mock() as created:
            url = stripe_billing.create_portal_session(self.user, flow=flow, plan=plan)
        self.assertEqual(url, "https://stripe/bps_x")
        return created.call_args.kwargs

    def test_no_flow_opens_the_portal_home(self):
        kwargs = self._portal_kwargs(subs=(self._sub(),))
        self.assertNotIn("flow_data", kwargs)
        self.assertEqual(kwargs["customer"], "cus_abc")

    def test_update_flow_targets_the_existing_subscription(self):
        kwargs = self._portal_kwargs(flow="update", plan="max", subs=(self._sub(),))
        confirm = kwargs["flow_data"]["subscription_update_confirm"]
        self.assertEqual(kwargs["flow_data"]["type"], "subscription_update_confirm")
        self.assertEqual(confirm["subscription"], "sub_x")
        # The subscription ITEM id, not the price id — Stripe rejects
        # anything else, and the two are easy to confuse.
        self.assertEqual(confirm["items"][0]["id"], "si_x")
        self.assertEqual(confirm["items"][0]["price"], "price_max_456")

    def test_update_flow_preserves_quantity(self):
        """Team subscriptions carry seats as `quantity`; a confirm flow
        that dropped it would resize the team as a side effect of a
        plan change."""
        kwargs = self._portal_kwargs(flow="update", plan="pro", subs=(self._sub(quantity=7),))
        confirm = kwargs["flow_data"]["subscription_update_confirm"]
        self.assertEqual(confirm["items"][0]["quantity"], 7)

    def test_update_flow_never_offers_a_legacy_price(self):
        """Grandfathered ids resolve INBOUND only. Switching plan must
        move the subscriber onto the current price — selling into a
        retired one would re-grandfather them at today's click."""
        kwargs = self._portal_kwargs(flow="update", plan="pro", subs=(self._sub(),))
        confirm = kwargs["flow_data"]["subscription_update_confirm"]
        self.assertEqual(confirm["items"][0]["price"], "price_pro_123")

    def test_switch_flow_is_the_picker_and_names_no_price(self):
        """The team row has one row for ALL tiers, so it can't name a
        target plan; the picker also leaves `quantity` (= seats) to
        Stripe rather than restating it."""
        kwargs = self._portal_kwargs(flow="switch", subs=(self._sub(quantity=7),))
        self.assertEqual(kwargs["flow_data"]["type"], "subscription_update")
        self.assertEqual(kwargs["flow_data"]["subscription_update"]["subscription"], "sub_x")
        self.assertNotIn("items", kwargs["flow_data"]["subscription_update"])

    def test_switch_flow_needs_no_plan(self):
        res = self.client.post(PORTAL_URL, {"flow": "switch"}, format="json")
        self.assertNotEqual(res.status_code, 400)

    def test_cancel_flow(self):
        kwargs = self._portal_kwargs(flow="cancel", subs=(self._sub(),))
        self.assertEqual(kwargs["flow_data"]["type"], "subscription_cancel")
        self.assertEqual(kwargs["flow_data"]["subscription_cancel"]["subscription"], "sub_x")

    def test_flow_returns_the_browser_to_the_app(self):
        """A deep-linked flow otherwise ends on Stripe's own screen; the
        redirect is what triggers the in-app reconcile."""
        kwargs = self._portal_kwargs(flow="cancel", subs=(self._sub(),))
        after = kwargs["flow_data"]["after_completion"]
        self.assertEqual(after["type"], "redirect")
        self.assertIn(stripe_billing.RETURN_PATH, after["redirect"]["return_url"])

    def test_no_live_subscription_degrades_to_the_portal_home(self):
        kwargs = self._portal_kwargs(flow="update", plan="max", subs=(self._sub("canceled"),))
        self.assertNotIn("flow_data", kwargs)

    def test_rejected_flow_falls_back_to_the_portal_home(self):
        """The portal CONFIGURATION (a dashboard setting, invisible from
        here) can refuse a plan change. Landing the user on the portal
        home still lets them act; a 503 doesn't."""
        import stripe  # noqa: PLC0415

        ok = stripe.billing_portal.Session.construct_from(
            {"id": "bps_x", "object": "billing_portal.session", "url": "https://stripe/bps_x"},
            "sk_test_x",
        )
        create = mock.patch(
            "stripe.billing_portal.Session.create",
            side_effect=[Exception("no such configuration feature"), ok],
        )
        kwargs = self._portal_kwargs(flow="update", plan="max", subs=(self._sub(),), create=create)
        self.assertNotIn("flow_data", kwargs)

    def test_view_rejects_an_unknown_flow(self):
        res = self.client.post(PORTAL_URL, {"flow": "delete_everything"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_view_rejects_update_without_a_purchasable_plan(self):
        for body in ({"flow": "update"}, {"flow": "update", "plan": "enterprise"}):
            res = self.client.post(PORTAL_URL, body, format="json")
            self.assertEqual(res.status_code, 400, body)

    def test_view_passes_the_flow_through(self):
        with mock.patch.object(
            stripe_billing, "create_portal_session", return_value="https://stripe/bps_1"
        ) as create:
            res = self.client.post(PORTAL_URL, {"flow": "update", "plan": "max"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(create.call_args.kwargs, {"flow": "update", "plan": "max"})


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class WebhookViewTests(BillingTestBase):
    def test_bad_signature_400(self):
        with mock.patch.object(
            stripe_billing,
            "verify_webhook",
            side_effect=stripe_billing.BillingError("bad sig"),
        ):
            res = self.client.post(WEBHOOK_URL, data=b"{}", content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_verified_event_applies_and_acks(self):
        event = self.checkout_completed_event(plan="pro")
        with mock.patch.object(stripe_billing, "verify_webhook", return_value=event):
            res = self.client.post(
                WEBHOOK_URL,
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=x",
            )
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertEqual(self.user.stripe_customer_id, "cus_abc")

    def test_handler_crash_still_acks_200(self):
        with (
            mock.patch.object(stripe_billing, "verify_webhook", return_value={"id": "evt_x"}),
            mock.patch.object(stripe_billing, "handle_event", side_effect=RuntimeError("bug")),
        ):
            res = self.client.post(WEBHOOK_URL, data=b"{}", content_type="application/json")
        self.assertEqual(res.status_code, 200)


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class VerifyWebhookRealSdkTests(BillingTestBase):
    """The REAL `stripe.Webhook.construct_event` — no mock.

    Regression: every other test here mocks `verify_webhook` and feeds
    `handle_event` a plain dict, so the whole suite passed while the
    production path 500'd on the first real webhook. `construct_event`
    returns a `stripe.Event` (`StripeObject`), which is NOT a dict
    subclass in stripe 5.x+, so `event.get(...)` raised AttributeError.
    These tests pin the contract `handle_event` actually relies on:
    verify_webhook returns PLAIN nested dicts, whatever the SDK's
    object model does next.
    """

    def signed(self, payload_dict) -> tuple[bytes, str]:
        """Body + a genuinely valid Stripe-Signature header for it."""
        body = json.dumps(payload_dict).encode()
        ts = int(time.time())
        secret = STRIPE_TEST_SETTINGS["WEBHOOK_SECRET"]
        sig = hmac.new(secret.encode(), b"%d." % ts + body, hashlib.sha256).hexdigest()
        return body, f"t={ts},v1={sig}"

    def event_payload(self, **over):
        payload = {
            "id": "evt_real_1",
            "object": "event",  # construct_event reads this
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": str(self.user.id),
                    "customer": "cus_real",
                    "metadata": {"plan": "pro"},
                }
            },
        }
        payload.update(over)
        return payload

    def test_returns_plain_nested_dicts(self):
        body, sig = self.signed(self.event_payload())
        event = stripe_billing.verify_webhook(body, sig)
        self.assertIs(type(event), dict)
        self.assertIs(type(event["data"]), dict)
        self.assertIs(type(event["data"]["object"]), dict)
        self.assertIs(type(event["data"]["object"]["metadata"]), dict)
        # The exact API handle_event + the view's error path use.
        self.assertEqual(event.get("type"), "checkout.session.completed")
        self.assertEqual(event.get("id"), "evt_real_1")
        self.assertEqual((event.get("data") or {}).get("object", {}).get("customer"), "cus_real")

    def test_real_event_flows_through_handle_event(self):
        """End-to-end on the real SDK output: the exact path that 500'd."""
        body, sig = self.signed(self.event_payload())
        event = stripe_billing.verify_webhook(body, sig)
        summary = stripe_billing.handle_event(event)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertEqual(self.user.stripe_customer_id, "cus_real")
        self.assertIn("pro", summary)

    def test_real_webhook_through_the_view(self):
        body, sig = self.signed(self.event_payload(type="customer.subscription.deleted"))
        # deleted → free; bind the customer first so it resolves.
        self.user.tier = "pro"
        self.user.stripe_customer_id = "cus_real"
        self.user.save(update_fields=["tier", "stripe_customer_id"])
        res = self.client.post(
            WEBHOOK_URL, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig
        )
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")

    def test_tampered_body_rejected(self):
        body, sig = self.signed(self.event_payload())
        with self.assertRaises(stripe_billing.BillingError):
            stripe_billing.verify_webhook(body + b" ", sig)

    def test_wrong_secret_rejected(self):
        body, sig = self.signed(self.event_payload())
        with override_settings(STRIPE={**STRIPE_TEST_SETTINGS, "WEBHOOK_SECRET": "whsec_other"}):
            with self.assertRaises(stripe_billing.BillingError):
                stripe_billing.verify_webhook(body, sig)


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class HandleEventTests(BillingTestBase):
    def _bind(self, customer="cus_abc"):
        self.user.stripe_customer_id = customer
        self.user.save(update_fields=["stripe_customer_id"])

    def test_checkout_completed_sets_tier_and_customer(self):
        summary = stripe_billing.handle_event(self.checkout_completed_event(plan="max"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")
        self.assertEqual(self.user.stripe_customer_id, "cus_abc")
        self.assertIn("max", summary)
        # Effective tier resolves immediately (cache evicted on write).
        self.assertEqual(quota.get_effective_tier(self.user.id), "max")

    def test_checkout_completed_unknown_user_ignored(self):
        event = self.checkout_completed_event()
        event["data"]["object"]["client_reference_id"] = "00000000-0000-0000-0000-000000000000"
        summary = stripe_billing.handle_event(event)
        self.assertIn("ignored", summary)

    def test_checkout_completed_bad_metadata_defers_tier(self):
        summary = stripe_billing.handle_event(self.checkout_completed_event(plan="enterprise"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")  # unchanged
        self.assertEqual(self.user.stripe_customer_id, "cus_abc")  # still bound
        self.assertIn("deferred", summary)

    def test_subscription_active_maps_price_to_tier(self):
        self._bind()
        stripe_billing.handle_event(
            self.subscription_event(
                etype="customer.subscription.updated", status_="active", price="price_max_456"
            )
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")

    def test_subscription_active_unmapped_price_no_change(self):
        self._bind()
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        summary = stripe_billing.handle_event(
            self.subscription_event(
                etype="customer.subscription.updated", status_="active", price="price_other"
            )
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertIn("unmapped", summary)

    def test_past_due_keeps_tier(self):
        self._bind()
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        stripe_billing.handle_event(
            self.subscription_event(etype="customer.subscription.updated", status_="past_due")
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")

    def test_unpaid_downgrades_to_free(self):
        self._bind()
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        stripe_billing.handle_event(
            self.subscription_event(etype="customer.subscription.updated", status_="unpaid")
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")

    def test_subscription_deleted_downgrades_to_free(self):
        self._bind()
        self.user.tier = "max"
        self.user.save(update_fields=["tier"])
        stripe_billing.handle_event(
            self.subscription_event(etype="customer.subscription.deleted", status_="canceled")
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")
        self.assertEqual(quota.get_effective_tier(self.user.id), "free")

    def test_unknown_customer_ignored(self):
        summary = stripe_billing.handle_event(
            self.subscription_event(
                etype="customer.subscription.updated", status_="active", customer="cus_nobody"
            )
        )
        self.assertIn("ignored", summary)

    def test_events_are_idempotent(self):
        event = self.checkout_completed_event(plan="pro")
        stripe_billing.handle_event(event)
        stripe_billing.handle_event(event)  # at-least-once delivery
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")

    def test_unknown_event_type_ignored(self):
        summary = stripe_billing.handle_event({"type": "invoice.paid", "data": {"object": {}}})
        self.assertIn("ignored", summary)


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class ReconcileTests(BillingTestBase):
    """`reconcile_from_stripe` — the pull-based repair for lost webhooks.

    The `Subscription.list` mocks return REAL SDK objects
    (`stripe.ListObject.construct_from`), never plain dicts: the service
    JSON-renders whatever the SDK hands back, and a plain-dict mock
    would assert a shape the SDK doesn't produce — the exact mistake
    that shipped the webhook 500. These tests double as the pin on
    `str(StripeObject)` being a JSON rendering.
    """

    def _bind(self, tier="free", customer="cus_abc"):
        self.user.tier = tier
        self.user.stripe_customer_id = customer
        self.user.save(update_fields=["tier", "stripe_customer_id"])

    @staticmethod
    def _sub(status_="active", price="price_pro_123"):
        return {
            "id": "sub_x",
            "object": "subscription",
            "status": status_,
            "items": {"data": [{"price": {"id": price}}]},
        }

    @staticmethod
    def _list_mock(*subs):
        import stripe  # noqa: PLC0415 — lazy like the service itself

        payload = {
            "object": "list",
            "data": list(subs),
            "has_more": False,
            "url": "/v1/subscriptions",
        }
        return mock.patch(
            "stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(payload, "sk_test_x"),
        )

    def test_no_customer_is_noop(self):
        summary = stripe_billing.reconcile_from_stripe(self.user)
        self.assertIn("no billing account", summary)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")

    def test_active_subscription_sets_tier(self):
        self._bind(tier="pro")
        with self._list_mock(self._sub(price="price_max_456")) as listed:
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")
        self.assertIn("max", summary)
        # Effective tier resolves immediately (cache evicted on write).
        self.assertEqual(quota.get_effective_tier(self.user.id), "max")
        self.assertEqual(listed.call_args.kwargs["customer"], "cus_abc")
        self.assertEqual(listed.call_args.kwargs["status"], "all")

    def test_best_of_multiple_active_wins(self):
        self._bind()
        with self._list_mock(self._sub(price="price_pro_123"), self._sub(price="price_max_456")):
            stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")

    def test_all_canceled_downgrades_to_free(self):
        self._bind(tier="max")
        with self._list_mock(self._sub(status_="canceled", price="price_max_456")):
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")
        self.assertIn("free", summary)

    def test_no_subscriptions_downgrades_to_free(self):
        self._bind(tier="pro")
        with self._list_mock():
            stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")

    def test_past_due_only_keeps_tier(self):
        self._bind(tier="pro")
        with self._list_mock(self._sub(status_="past_due")):
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertIn("unchanged", summary)

    def test_active_unmapped_price_unchanged(self):
        self._bind(tier="pro")
        with self._list_mock(self._sub(price="price_other")):
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertIn("unmapped", summary)

    def test_enterprise_never_touched(self):
        self._bind(tier="enterprise")
        with self._list_mock(self._sub(status_="canceled")) as listed:
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "enterprise")
        self.assertIn("operator-managed", summary)
        listed.assert_not_called()

    def test_stripe_error_raises_billing_error(self):
        self._bind()
        with mock.patch("stripe.Subscription.list", side_effect=RuntimeError("api down")):
            with self.assertRaises(stripe_billing.BillingError):
                stripe_billing.reconcile_from_stripe(self.user)


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class RefreshViewTests(BillingTestBase):
    def test_refresh_applies_and_returns_tier(self):
        def fake_reconcile(user):
            user.tier = "max"
            user.save(update_fields=["tier"])
            return "tier set to max"

        with mock.patch.object(stripe_billing, "reconcile_from_stripe", side_effect=fake_reconcile):
            res = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["personal_tier"], "max")
        self.assertIn("max", res.data["detail"])

    def test_refresh_billing_error_maps_to_503(self):
        with mock.patch.object(
            stripe_billing,
            "reconcile_from_stripe",
            side_effect=stripe_billing.BillingError("boom"),
        ):
            res = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 503)

    def test_refresh_disabled_with_customer_503(self):
        # No mocking: `_stripe()` itself raises with an empty SECRET_KEY.
        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            res = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 503)

    def test_refresh_disabled_without_customer_noops_200(self):
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            res = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no billing account", res.data["detail"])


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class SubscriptionOverviewTests(BillingTestBase):
    """`subscription_overview` + GET /billing/subscription/.

    Mocks return real SDK `ListObject`s — see `ReconcileTests` for why
    plain dicts are banned here.
    """

    def _bind(self, customer="cus_abc"):
        self.user.stripe_customer_id = customer
        self.user.save(update_fields=["stripe_customer_id"])

    @staticmethod
    def _sub(
        status_="active",
        price="price_pro_123",
        created=100,
        cancel_at_period_end=False,
        cancel_at=None,
        item_period_end=1900000000,
        top_period_end=None,
    ):
        sub = {
            "id": f"sub_{status_}_{created}",
            "object": "subscription",
            "status": status_,
            "created": created,
            "cancel_at_period_end": cancel_at_period_end,
            "cancel_at": cancel_at,
            "items": {"data": [{"price": {"id": price}, "current_period_end": item_period_end}]},
        }
        if top_period_end is not None:
            sub["current_period_end"] = top_period_end
        return sub

    @staticmethod
    def _list_mock(*subs):
        import stripe  # noqa: PLC0415 — lazy like the service itself

        payload = {
            "object": "list",
            "data": list(subs),
            "has_more": False,
            "url": "/v1/subscriptions",
        }
        return mock.patch(
            "stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(payload, "sk_test_x"),
        )

    def test_no_customer_is_none(self):
        self.assertIsNone(stripe_billing.subscription_overview(self.user))

    def test_disabled_is_none_even_with_customer(self):
        self._bind()
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            self.assertIsNone(stripe_billing.subscription_overview(self.user))

    def test_active_subscription_reads_item_period_end(self):
        self._bind()
        with self._list_mock(self._sub(price="price_max_456", item_period_end=1900000123)):
            o = stripe_billing.subscription_overview(self.user)
        self.assertEqual(o["plan"], "max")
        self.assertEqual(o["status"], "active")
        self.assertFalse(o["cancel_at_period_end"])
        # API 2025-03-31+ shape: period end lives on the item.
        self.assertEqual(o["current_period_end"], 1900000123)

    def test_top_level_period_end_fallback(self):
        self._bind()
        with self._list_mock(self._sub(item_period_end=None, top_period_end=1900000456)):
            o = stripe_billing.subscription_overview(self.user)
        self.assertEqual(o["current_period_end"], 1900000456)

    def test_scheduled_cancellation_passes_through(self):
        self._bind()
        with self._list_mock(self._sub(cancel_at_period_end=True, cancel_at=1900000789)):
            o = stripe_billing.subscription_overview(self.user)
        self.assertTrue(o["cancel_at_period_end"])
        self.assertEqual(o["cancel_at"], 1900000789)

    def test_only_terminal_subscriptions_is_none(self):
        self._bind()
        with self._list_mock(self._sub(status_="canceled")):
            self.assertIsNone(stripe_billing.subscription_overview(self.user))

    def test_active_preferred_over_past_due_then_newest(self):
        self._bind()
        with self._list_mock(
            self._sub(status_="past_due", price="price_max_456", created=300),
            self._sub(status_="active", price="price_pro_123", created=100),
            self._sub(status_="active", price="price_max_456", created=200),
        ):
            o = stripe_billing.subscription_overview(self.user)
        self.assertEqual(o["status"], "active")
        self.assertEqual(o["plan"], "max")  # newest active wins

    def test_view_returns_payload(self):
        self._bind()
        with self._list_mock(self._sub(price="price_pro_123")):
            res = self.client.get(SUBSCRIPTION_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["subscription"]["plan"], "pro")

    def test_view_null_without_customer(self):
        res = self.client.get(SUBSCRIPTION_URL)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["subscription"])

    def test_view_billing_error_maps_to_503(self):
        self._bind()
        with mock.patch("stripe.Subscription.list", side_effect=RuntimeError("api down")):
            res = self.client.get(SUBSCRIPTION_URL)
        self.assertEqual(res.status_code, 503)


@override_settings(STRIPE={**STRIPE_TEST_SETTINGS, "SECRET_KEY": "pk_live_x"})
class WrongKeyKindTests(BillingTestBase):
    """A publishable key in STRIPE_SECRET_KEY — the dashboard
    copy-paste mix-up that actually shipped to prod (the pk sits
    directly above the sk in the dashboard). It must read as
    billing-DISABLED with a clear reason, not as healthy config that
    503s in a user's face at click time."""

    def test_billing_disabled_with_pk_key(self):
        self.assertFalse(stripe_billing.billing_enabled())

    def test_config_endpoint_reports_disabled(self):
        res = self.client.get(CONFIG_URL)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["enabled"])

    def test_checkout_503_names_the_problem(self):
        res = self.client.post(CHECKOUT_URL, {"plan": "pro"}, format="json")
        self.assertEqual(res.status_code, 503)
        self.assertIn("publishable", res.data["error"])

    def test_restricted_key_is_accepted(self):
        with override_settings(STRIPE={**STRIPE_TEST_SETTINGS, "SECRET_KEY": "rk_test_x"}):
            self.assertTrue(stripe_billing.billing_enabled())

    def test_missing_key_still_just_disabled(self):
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            self.assertFalse(stripe_billing.billing_enabled())


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class EnsureCustomerTests(BillingTestBase):
    """`ensure_customer` self-heal: a customer deleted in the Stripe
    dashboard used to brick that user's checkout permanently (every
    session create failed `resource_missing` until an operator nulled
    the column). Gone-customers are replaced; ambiguous failures are
    NOT (a replacement minted on a transient error would detach the
    user from the customer their live subscription bills against)."""

    @staticmethod
    def _customer(id_="cus_new", deleted=False):
        import stripe  # noqa: PLC0415

        payload = {"id": id_, "object": "customer"}
        if deleted:
            payload["deleted"] = True
        return stripe.Customer.construct_from(payload, "sk_test_x")

    def _bind(self, customer="cus_old"):
        self.user.stripe_customer_id = customer
        self.user.save(update_fields=["stripe_customer_id"])

    def test_live_customer_is_reused(self):
        self._bind()
        with (
            mock.patch("stripe.Customer.retrieve", return_value=self._customer("cus_old")),
            mock.patch("stripe.Customer.create") as create,
        ):
            self.assertEqual(stripe_billing.ensure_customer(self.user), "cus_old")
        create.assert_not_called()

    def test_deleted_customer_is_replaced(self):
        self._bind()
        with (
            mock.patch(
                "stripe.Customer.retrieve",
                return_value=self._customer("cus_old", deleted=True),
            ),
            mock.patch("stripe.Customer.create", return_value=self._customer("cus_new")),
        ):
            self.assertEqual(stripe_billing.ensure_customer(self.user), "cus_new")
        self.user.refresh_from_db()
        self.assertEqual(self.user.stripe_customer_id, "cus_new")

    def test_resource_missing_is_replaced(self):
        import stripe  # noqa: PLC0415

        self._bind()
        with (
            mock.patch(
                "stripe.Customer.retrieve",
                side_effect=stripe.InvalidRequestError(
                    "No such customer", param="customer", code="resource_missing"
                ),
            ),
            mock.patch("stripe.Customer.create", return_value=self._customer("cus_new")),
        ):
            self.assertEqual(stripe_billing.ensure_customer(self.user), "cus_new")
        self.user.refresh_from_db()
        self.assertEqual(self.user.stripe_customer_id, "cus_new")

    def test_transient_error_does_not_replace(self):
        self._bind()
        with (
            mock.patch("stripe.Customer.retrieve", side_effect=RuntimeError("api down")),
            mock.patch("stripe.Customer.create") as create,
        ):
            with self.assertRaises(stripe_billing.BillingError):
                stripe_billing.ensure_customer(self.user)
        create.assert_not_called()
        self.user.refresh_from_db()
        # The binding survives — webhooks for the live subscription
        # must still resolve to this user.
        self.assertEqual(self.user.stripe_customer_id, "cus_old")

    def test_no_customer_creates_and_binds(self):
        with (
            mock.patch("stripe.Customer.retrieve") as retrieve,
            mock.patch("stripe.Customer.create", return_value=self._customer("cus_new")),
        ):
            self.assertEqual(stripe_billing.ensure_customer(self.user), "cus_new")
        retrieve.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.stripe_customer_id, "cus_new")


TEAM_CONFIG_URL = "/api/v2/billing/team/config/"
TEAM_CHECKOUT_URL = "/api/v2/billing/team/checkout/"
TEAM_PORTAL_URL = "/api/v2/billing/team/portal/"


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class TeamBillingTests(BillingTestBase):
    """Per-seat team subscriptions: owner-gated endpoints, the
    quantity-based checkout, team webhook resolution, seat auto-sync,
    and the team reconcile. `self.user` owns `self.team`; `self.user2`
    is a plain member."""

    def _bind_team(self, customer="cus_team_1"):
        self.team.stripe_customer_id = customer
        self.team.save(update_fields=["stripe_customer_id"])

    @staticmethod
    def _sdk(kind, payload):
        import stripe  # noqa: PLC0415

        cls = stripe
        for part in kind.split("."):
            cls = getattr(cls, part)
        return cls.construct_from(payload, "sk_test_x")

    def _team_checkout_event(self, *, plan="pro", customer="cus_team_1"):
        return {
            "id": "evt_t1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": customer,
                    "metadata": {"genos_team_id": str(self.team.team_id), "plan": plan},
                }
            },
        }

    # ---- endpoints -------------------------------------------------- #

    def test_config_lists_owned_teams_with_seats(self):
        res = self.client.get(TEAM_CONFIG_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["teams"]), 1)
        t = res.data["teams"][0]
        self.assertEqual(t["team_name"], "Test Team")
        self.assertEqual(t["seats"], 2)
        self.assertEqual(t["plan"], "free")
        self.assertFalse(t["has_billing_account"])

    def test_config_empty_for_non_owner(self):
        self.authenticate(self.user2)
        res = self.client.get(TEAM_CONFIG_URL)
        self.assertEqual(res.data["teams"], [])

    def test_checkout_owner_only_404_for_member(self):
        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_CHECKOUT_URL,
            {"team_id": str(self.team.team_id), "plan": "pro"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_checkout_unknown_plan_400(self):
        res = self.client.post(
            TEAM_CHECKOUT_URL,
            {"team_id": str(self.team.team_id), "plan": "enterprise"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_checkout_owner_gets_url(self):
        with mock.patch.object(
            stripe_billing, "create_team_checkout_session", return_value="https://stripe/cs_t"
        ) as create:
            res = self.client.post(
                TEAM_CHECKOUT_URL,
                {"team_id": str(self.team.team_id), "plan": "max"},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["url"], "https://stripe/cs_t")
        self.assertEqual(create.call_args.args[1], "max")

    def test_portal_owner_only(self):
        self.authenticate(self.user2)
        res = self.client.post(TEAM_PORTAL_URL, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 404)

    # ---- checkout session shape ------------------------------------- #

    def test_team_session_quantity_and_metadata(self):
        session = self._sdk(
            "checkout.Session",
            {"id": "cs_t", "object": "checkout.session", "url": "https://stripe/cs_t"},
        )
        with (
            mock.patch(
                "stripe.Customer.create",
                return_value=self._sdk("Customer", {"id": "cus_team_1", "object": "customer"}),
            ),
            mock.patch("stripe.checkout.Session.create", return_value=session) as create,
        ):
            url = stripe_billing.create_team_checkout_session(self.team, "pro")
        self.assertEqual(url, "https://stripe/cs_t")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["line_items"][0]["quantity"], 2)  # both members
        self.assertEqual(kwargs["metadata"]["genos_team_id"], str(self.team.team_id))
        self.team.refresh_from_db()
        self.assertEqual(self.team.stripe_customer_id, "cus_team_1")

    # ---- webhook ----------------------------------------------------- #

    def test_team_checkout_completed_sets_plan_and_member_tiers(self):
        summary = stripe_billing.handle_event(self._team_checkout_event(plan="pro"))
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "pro")
        self.assertEqual(self.team.stripe_customer_id, "cus_team_1")
        self.assertIn("team plan", summary)
        # A plain member inherits immediately (cache evicted on write).
        self.assertEqual(quota.get_effective_tier(self.user2.id), "pro")

    def test_team_subscription_updated_maps_price(self):
        self._bind_team()
        stripe_billing.handle_event(
            self.subscription_event(
                etype="customer.subscription.updated",
                status_="active",
                price="price_max_456",
                customer="cus_team_1",
            )
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "max")

    def test_team_subscription_deleted_downgrades(self):
        self._bind_team()
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        stripe_billing.handle_event(
            self.subscription_event(
                etype="customer.subscription.deleted",
                status_="canceled",
                customer="cus_team_1",
            )
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "free")
        self.assertEqual(quota.get_effective_tier(self.user2.id), "free")

    # ---- seat auto-sync ---------------------------------------------- #

    def _seat_sync_mocks(self, current_quantity=2):
        list_obj = self._sdk(
            "ListObject",
            {
                "object": "list",
                "data": [
                    {
                        "id": "sub_t1",
                        "object": "subscription",
                        "status": "active",
                        "items": {"data": [{"id": "si_1", "quantity": current_quantity}]},
                    }
                ],
                "has_more": False,
                "url": "/v1/subscriptions",
            },
        )
        return (
            mock.patch("stripe.Subscription.list", return_value=list_obj),
            mock.patch("stripe.Subscription.modify"),
        )

    def test_member_join_bumps_quantity(self):
        from origin.models.common.team_models import TeamMembers  # noqa: PLC0415
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        self._bind_team()
        user3 = CustomUser.objects.create_user(
            email="third@example.com", username="third", password="x" * 24
        )
        list_mock, modify_mock = self._seat_sync_mocks(current_quantity=2)
        with list_mock, modify_mock as modify:
            TeamMembers.objects.create(team=self.team, attendee=user3)  # signal fires
        modify.assert_called_once()
        self.assertEqual(modify.call_args.kwargs["items"][0]["quantity"], 3)
        self.assertEqual(modify.call_args.kwargs["proration_behavior"], "create_prorations")

    def test_sync_noop_when_already_true(self):
        self._bind_team()
        list_mock, modify_mock = self._seat_sync_mocks(current_quantity=2)
        with list_mock, modify_mock as modify:
            out = stripe_billing.sync_team_subscription_quantity(self.team.team_id)
        self.assertEqual(out, "in sync")
        modify.assert_not_called()

    def test_sync_fail_soft(self):
        self._bind_team()
        with mock.patch("stripe.Subscription.list", side_effect=RuntimeError("api down")):
            out = stripe_billing.sync_team_subscription_quantity(self.team.team_id)
        self.assertIn("failed", out)  # logged, never raised

    def test_sync_noop_without_billing_account(self):
        with mock.patch("stripe.Subscription.list") as listed:
            out = stripe_billing.sync_team_subscription_quantity(self.team.team_id)
        self.assertEqual(out, "no-op")
        listed.assert_not_called()

    # ---- reconcile ---------------------------------------------------- #

    def test_reconcile_team_active_sets_plan(self):
        self._bind_team()
        list_obj = self._sdk(
            "ListObject",
            {
                "object": "list",
                "data": [
                    {
                        "id": "sub_t1",
                        "object": "subscription",
                        "status": "active",
                        "items": {"data": [{"price": {"id": "price_max_456"}}]},
                    }
                ],
                "has_more": False,
                "url": "/v1/subscriptions",
            },
        )
        with mock.patch("stripe.Subscription.list", return_value=list_obj):
            summary = stripe_billing.reconcile_team_from_stripe(self.team)
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "max")
        self.assertIn("max", summary)
        self.assertEqual(quota.get_effective_tier(self.user2.id), "max")

    def test_refresh_endpoint_reconciles_owned_teams(self):
        self._bind_team()
        with (
            mock.patch.object(
                stripe_billing, "reconcile_from_stripe", return_value="no billing account"
            ),
            mock.patch.object(
                stripe_billing, "reconcile_team_from_stripe", return_value="team plan set to pro"
            ) as team_rec,
        ):
            res = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(res.status_code, 200)
        team_rec.assert_called_once()


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class PlansViewTests(BillingTestBase):
    """GET /billing/plans/ + `price_display`.

    Price mocks return real SDK objects (`stripe.Price.construct_from`)
    — same discipline as the other real-SDK mocks in this file.
    """

    def setUp(self):
        super().setUp()
        # price_display caches per price id — evict so one test's mock
        # can't satisfy the next test from cache.
        from django.core.cache import cache  # noqa: PLC0415

        cache.delete("stripe_price_display:price_pro_123")
        cache.delete("stripe_price_display:price_max_456")

    @staticmethod
    def _price_mock(amount=1200, currency="jpy"):
        import stripe  # noqa: PLC0415

        return mock.patch(
            "stripe.Price.retrieve",
            return_value=stripe.Price.construct_from(
                {
                    "id": "price_x",
                    "object": "price",
                    "unit_amount": amount,
                    "currency": currency,
                    "recurring": {"interval": "month"},
                },
                "sk_test_x",
            ),
        )

    def test_price_display_reads_stripe(self):
        with self._price_mock(amount=2500):
            self.assertEqual(
                stripe_billing.price_display("max"),
                {"amount": 2500, "currency": "jpy", "interval": "month"},
            )

    def test_price_display_fail_soft(self):
        with mock.patch("stripe.Price.retrieve", side_effect=RuntimeError("api down")):
            self.assertIsNone(stripe_billing.price_display("pro"))

    def test_price_display_none_when_disabled(self):
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            self.assertIsNone(stripe_billing.price_display("pro"))

    def test_plans_payload_mirrors_tier_quotas(self):
        from django.conf import settings as dj_settings  # noqa: PLC0415

        with self._price_mock():
            res = self.client.get(PLANS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["billing_enabled"])
        tiers = {t["tier"]: t for t in res.data["tiers"]}
        self.assertEqual(list(tiers), ["free", "core", "pro", "max", "enterprise"])
        quotas = dj_settings.SEARCH_ENGINE["TIER_QUOTAS"]
        for name, t in tiers.items():
            self.assertEqual(t["limits"]["llm_ask_daily"], quotas[name]["llm_ask_daily"])
            self.assertEqual(
                t["limits"]["message_retention_days"], quotas[name]["message_retention_days"]
            )
        self.assertNotIn("model_daily", tiers["pro"]["limits"])

    def test_plans_payload_includes_capability_keys(self):
        # The UX-pillar keys ride the same tier-config-only pipeline as
        # the classic six — safe on this public view precisely because
        # they are per-TIER values, never per-user.
        from django.conf import settings as dj_settings  # noqa: PLC0415

        with self._price_mock():
            res = self.client.get(PLANS_URL)
        self.assertEqual(res.status_code, 200)
        quotas = dj_settings.SEARCH_ENGINE["TIER_QUOTAS"]
        for t in res.data["tiers"]:
            limits, cfg = t["limits"], quotas[t["tier"]]
            self.assertEqual(limits["agent_tool_level"], cfg["agent_tool_level"])
            self.assertEqual(limits["max_effort"], cfg["max_effort"])
            self.assertEqual(limits["auto_effort"], cfg["auto_effort"])
            self.assertEqual(limits["agent_memory"], cfg["agent_memory"])
            self.assertEqual(
                limits["agent_history_retention_days"],
                cfg["agent_history_retention_days"],
            )
            self.assertEqual(limits["integrations"], cfg["integrations"])
            self.assertEqual(limits["digest_cadence"], cfg["digest_cadence"])

    def test_plans_flags_and_prices(self):
        with self._price_mock():
            res = self.client.get(PLANS_URL)
        tiers = {t["tier"]: t for t in res.data["tiers"]}
        self.assertFalse(tiers["free"]["purchasable"])
        self.assertEqual(tiers["free"]["price"]["amount"], 0)
        self.assertTrue(tiers["pro"]["purchasable"])
        self.assertTrue(tiers["max"]["purchasable"])
        self.assertFalse(tiers["enterprise"]["purchasable"])
        self.assertTrue(tiers["enterprise"]["contact_sales"])
        self.assertIsNone(tiers["enterprise"]["price"])

    def test_plans_render_without_stripe(self):
        with override_settings(STRIPE=STRIPE_DISABLED_SETTINGS):
            res = self.client.get(PLANS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["billing_enabled"])
        tiers = {t["tier"]: t for t in res.data["tiers"]}
        # Limits still render; paid prices are null; nothing purchasable.
        self.assertEqual(len(tiers), 5)
        self.assertIsNone(tiers["pro"]["price"])
        self.assertFalse(tiers["pro"]["purchasable"])

    def test_plans_public_without_auth(self):
        # The public marketing pricing page renders this while logged
        # out, so the endpoint must answer 200 with no Authorization.
        self.client.credentials()  # drop the Bearer set in setUp
        with self._price_mock():
            res = self.client.get(PLANS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            [t["tier"] for t in res.data["tiers"]],
            ["free", "core", "pro", "max", "enterprise"],
        )


# --------------------------------------------------------------------------- #
# Multi-currency subscription pricing                                          #
# --------------------------------------------------------------------------- #

MULTI_CURRENCY_SETTINGS = {
    **STRIPE_TEST_SETTINGS,
    "PRICES_BY_CURRENCY": {
        "usd": {"core": "price_usd_core", "pro": "price_usd_pro"},
        "eur": {"core": "price_eur_core"},
    },
}


@override_settings(STRIPE=MULTI_CURRENCY_SETTINGS)
class MultiCurrencyPricingTests(BillingTestBase):
    """Selling in more than one currency.

    Prices are DECLARED per currency, never converted: `$9` and `¥1,200`
    are both round numbers a human chose to read well locally, and
    neither is the other one times an exchange rate. This is the one
    place the cost system's "USD base, convert for display" rule does
    not apply — converting here would advertise ¥1,847/month and move it
    with the market.
    """

    def test_the_default_currency_uses_the_plain_price_settings(self):
        self.assertEqual(stripe_billing.default_currency(), "jpy")
        self.assertEqual(stripe_billing.price_for_plan("core"), "price_core_789")

    def test_another_currency_resolves_its_own_declared_price(self):
        self.assertEqual(stripe_billing.price_for_plan("core", "usd"), "price_usd_core")
        self.assertEqual(stripe_billing.price_for_plan("core", "eur"), "price_eur_core")

    def test_a_plan_not_yet_priced_in_a_currency_is_not_offered(self):
        """Rather than offered and then failing at checkout — which is
        an upgrade button that leads nowhere."""
        self.assertIsNone(stripe_billing.price_for_plan("max", "eur"))
        self.assertEqual(stripe_billing.purchasable_plans("eur"), ["core"])
        self.assertEqual(stripe_billing.purchasable_plans("usd"), ["core", "pro"])
        self.assertEqual(stripe_billing.purchasable_plans("jpy"), ["core", "pro", "max"])

    def test_supported_currencies_are_derived_from_configured_prices(self):
        """Declared lists go stale; a currency must not appear on the
        pricing page before its Stripe prices exist."""
        self.assertEqual(sorted(stripe_billing.supported_currencies()), ["eur", "jpy", "usd"])
        self.assertEqual(stripe_billing.supported_currencies()[0], "jpy", "default comes first")

    def test_tier_for_price_resolves_every_currency(self):
        """A euro subscriber's renewal carries the euro price id. If only
        the default currency resolved, every renewal outside Japan would
        log 'unmapped price' and quietly stop maintaining that customer's
        tier."""
        self.assertEqual(stripe_billing.tier_for_price("price_core_789"), "core")
        self.assertEqual(stripe_billing.tier_for_price("price_usd_pro"), "pro")
        self.assertEqual(stripe_billing.tier_for_price("price_eur_core"), "core")
        self.assertIsNone(stripe_billing.tier_for_price("price_unknown"))

    def test_grandfathered_ids_still_resolve(self):
        """The currency axis must not have displaced the legacy one."""
        self.assertEqual(stripe_billing.tier_for_price("price_pro_old_1200"), "pro")

    def test_checkout_sells_the_price_for_the_requested_currency(self):
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return {"url": "https://checkout.example/x"}

        with mock.patch.object(stripe_billing, "_stripe") as m:
            m.return_value.checkout.Session.create.side_effect = _fake_create
            m.return_value.Customer.create.return_value = {"id": "cus_x"}
            stripe_billing.create_checkout_session(self.user, "pro", "usd")
        self.assertEqual(captured["line_items"][0]["price"], "price_usd_pro")

    def test_checkout_refuses_a_currency_the_plan_has_no_price_in(self):
        """Loudly, and naming the currency — a silent fall back to the
        default would bill a euro buyer in yen."""
        with self.assertRaises(stripe_billing.BillingError) as ctx:
            stripe_billing.create_checkout_session(self.user, "max", "eur")
        self.assertIn("EUR", str(ctx.exception))


@override_settings(STRIPE=MULTI_CURRENCY_SETTINGS)
class PlansEndpointCurrencyTests(BillingTestBase):
    def test_plans_default_to_the_configured_currency(self):
        res = self.client.get("/api/v2/billing/plans/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["currency"], "jpy")
        self.assertEqual(sorted(res.data["supported_currencies"]), ["eur", "jpy", "usd"])

    def test_free_is_priced_in_the_REQUESTED_currency_not_hardcoded_yen(self):
        """It was `{"amount": 0, "currency": "jpy"}` regardless. The
        client formats with Intl, so a dollar visitor was shown '¥0'."""
        res = self.client.get("/api/v2/billing/plans/?currency=usd")
        free = next(t for t in res.data["tiers"] if t["tier"] == "free")
        self.assertEqual(free["price"], {"amount": 0, "currency": "usd", "interval": "month"})

    def test_an_unsupported_currency_falls_back_rather_than_erroring(self):
        """A bad query string should render a pricing page, not break one."""
        res = self.client.get("/api/v2/billing/plans/?currency=zzz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["currency"], "jpy")

    def test_only_plans_priced_in_that_currency_are_purchasable(self):
        res = self.client.get("/api/v2/billing/plans/?currency=eur")
        purchasable = [t["tier"] for t in res.data["tiers"] if t["purchasable"]]
        self.assertEqual(purchasable, ["core"])


def _se(**overrides):
    """SEARCH_ENGINE with overrides — this module had no such helper
    because nothing here needed the AI flags before credits."""
    from django.conf import settings as dj

    cfg = dict(dj.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class PlansCreditAllowanceTests(BillingTestBase):
    """`monthly_ai_credits` on the plans payload.

    The plans page used to advertise "N AI asks every day". Under
    credits those daily numbers still exist and are still counted — the
    Free abuse breaker reads one — but they LIMIT NOBODY, so a pricing
    page quoting them is selling a limit that is not one.

    Present only when the server actually enforces credits, so its
    presence is the client's render switch. Same payload-shape
    convention `credits` uses on /agent/features/, and for the same
    reason: either side can deploy first and the page still describes
    what is really enforced.
    """

    def _limits(self, tier: str) -> dict:
        res = self.client.get("/api/v2/billing/plans/")
        return next(t for t in res.data["tiers"] if t["tier"] == tier)["limits"]

    @override_settings(
        SEARCH_ENGINE=_se(
            AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True
        ),
        STRIPE=STRIPE_TEST_SETTINGS,
    )
    def test_allowance_is_served_when_credits_rule(self):
        self.assertEqual(self._limits("free")["monthly_ai_credits"], 5)
        self.assertEqual(self._limits("core")["monthly_ai_credits"], 30)
        self.assertEqual(self._limits("pro")["monthly_ai_credits"], 70)
        self.assertEqual(self._limits("max")["monthly_ai_credits"], 150)

    @override_settings(
        SEARCH_ENGINE=_se(
            AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True
        ),
        STRIPE=STRIPE_TEST_SETTINGS,
    )
    def test_unlimited_plan_is_null_not_absent(self):
        """null = unlimited; ABSENT = credits are not enforced at all.
        Conflating them would advertise "unlimited AI" on every plan the
        moment the flag went off."""
        limits = self._limits("enterprise")
        self.assertIn("monthly_ai_credits", limits)
        self.assertIsNone(limits["monthly_ai_credits"])

    @override_settings(
        SEARCH_ENGINE=_se(AI_CREDITS_AUTHORITATIVE=False), STRIPE=STRIPE_TEST_SETTINGS
    )
    def test_absent_when_credits_are_not_the_limit(self):
        self.assertNotIn("monthly_ai_credits", self._limits("pro"))

    @override_settings(
        SEARCH_ENGINE=_se(AI_CREDITS_SHADOW=False, AI_CREDITS_AUTHORITATIVE=True),
        STRIPE=STRIPE_TEST_SETTINGS,
    )
    def test_absent_without_the_ledger(self):
        """Authoritative WITHOUT the shadow ledger enforces nothing, so
        the page must not advertise credits either — the predicate is
        single-sourced in `credit_ledger.credits_authoritative` precisely
        so this cannot drift from the ask gate."""
        self.assertNotIn("monthly_ai_credits", self._limits("pro"))

    @override_settings(
        SEARCH_ENGINE=_se(
            AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True
        ),
        STRIPE=STRIPE_TEST_SETTINGS,
    )
    def test_the_daily_keys_still_ship(self):
        """They are still counted server-side and older clients still
        read them; the page simply stops SELLING them."""
        limits = self._limits("pro")
        self.assertIsNotNone(limits["llm_ask_daily"])
        self.assertIsNotNone(limits["web_search_daily"])


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class TierProvenanceTests(BillingTestBase):
    """An operator-set tier is not Stripe's to take away.

    `reconcile_from_stripe` rewrites the tier from what Stripe says, and
    "Stripe says nothing" means free — so before `tier_source` existed, a
    comped account was demoted the next time its owner came back from
    checkout or the portal. It stayed nearly unreachable only by
    accident (the reconcile bails at "no billing account" when there is
    no `stripe_customer_id`, and a comp that never bought anything had
    none) — until credit packs, whose `ensure_customer` binds a customer
    id to an account with no subscription: exactly the shape read as
    "lapsed → free".

    The asymmetry under test: a pin blocks the DEMOTION, never a real
    subscription. Anything else would leave a comped user who later
    subscribes holding a paid tier for free the day they cancel.

    Stripe mocks are the ones `ReconcileTests` documents — real SDK
    objects, because the service JSON-renders whatever the SDK hands
    back and a plain-dict mock asserts a shape Stripe doesn't produce.
    """

    _sub = staticmethod(ReconcileTests._sub)
    _list_mock = staticmethod(ReconcileTests._list_mock)

    def _pin(self, tier, source=TIER_SOURCE_OPERATOR, customer="cus_abc"):
        self.user.tier = tier
        self.user.tier_source = source
        self.user.stripe_customer_id = customer
        self.user.save(update_fields=["tier", "tier_source", "stripe_customer_id"])

    def _pin_team(self, plan, source=TIER_SOURCE_OPERATOR, customer="cus_team_1"):
        self.team.plan = plan
        self.team.plan_source = source
        self.team.stripe_customer_id = customer
        self.team.save(update_fields=["plan", "plan_source", "stripe_customer_id"])

    # ---- the reconcile ---------------------------------------------- #

    def test_reconcile_keeps_a_comped_tier(self):
        """The headline case: comped at max, no subscription, back from
        Stripe. Before provenance this read `free`."""
        self._pin("max")
        with self._list_mock():
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_OPERATOR)
        self.assertIn("unchanged", summary)
        self.assertEqual(quota.get_effective_tier(self.user.id), "max")

    def test_reconcile_still_demotes_a_stripe_set_tier(self):
        """The pin must not cost us the repair it protects against —
        a lapsed subscriber is still reset."""
        self._pin("max", source=TIER_SOURCE_STRIPE)
        with self._list_mock(self._sub(status_="canceled")):
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")
        self.assertEqual(summary, "tier set to free")

    def test_a_real_subscription_overrides_the_pin_and_releases_it(self):
        self._pin("max")
        with self._list_mock(self._sub(price="price_core_789")):
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "core")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_STRIPE)
        self.assertIn("core", summary)

    def test_subscribing_at_the_pinned_tier_still_releases_the_pin(self):
        """The leak that hides behind a no-op write: comped at pro, then
        actually subscribes to pro. The tier never moves, so a
        `previous == tier` early return would leave the pin on — and the
        day they cancel, they keep pro for free."""
        self._pin("pro")
        with self._list_mock(self._sub(price="price_pro_123")):
            stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_STRIPE)

        # ...and now the demotion lands, because billing owns it again.
        with self._list_mock():
            stripe_billing.reconcile_from_stripe(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")

    def test_summary_never_claims_a_write_that_did_not_happen(self):
        """The summary is logged, echoed in the webhook's 200 body, and
        returned to the browser by `/billing/refresh/`. "tier set to
        free" beside a row that still says max is how a support ticket
        becomes an hour of log reading."""
        self._pin("max")
        with self._list_mock():
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.assertNotIn("set to free", summary)
        self.assertIn("max", summary)

    def test_summary_is_honest_when_the_tier_was_already_free(self):
        """No pin, nothing to do: still not "tier set to free", because
        nothing was set."""
        self._pin("free", source=TIER_SOURCE_STRIPE)
        with self._list_mock():
            summary = stripe_billing.reconcile_from_stripe(self.user)
        self.assertEqual(summary, "unchanged: tier stays free")

    # ---- webhooks ---------------------------------------------------- #

    def test_subscription_deleted_keeps_a_comped_tier(self):
        self._pin("pro")
        summary = stripe_billing.handle_event(
            {
                "id": "evt_d",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": "cus_abc"}},
            }
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertIn("unchanged", summary)

    def test_terminal_status_keeps_a_comped_tier(self):
        self._pin("pro")
        summary = stripe_billing.handle_event(
            self.subscription_event(etype="customer.subscription.updated", status_="canceled")
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertIn("unchanged", summary)

    def test_checkout_completed_releases_the_pin(self):
        self._pin("core")
        stripe_billing.handle_event(self.checkout_completed_event(plan="max"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_STRIPE)

    def test_enterprise_survives_a_deleted_webhook(self):
        """The reconcile always skipped enterprise; `handle_event` never
        did, so a stray terminal event could demote a contract account.
        Migration 0182 pins every existing enterprise row, which closes
        it — that backfill is only sound because no configured price maps
        to enterprise (pinned below)."""
        self._pin("enterprise")
        stripe_billing.handle_event(
            {
                "id": "evt_d2",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": "cus_abc"}},
            }
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "enterprise")

    def test_no_price_maps_to_enterprise(self):
        """The premise migration 0182 backfills on: an `enterprise` row
        can only have been set by hand. `tier_for_price` iterates
        PURCHASABLE_PLANS, and the checkout arm refuses any plan outside
        it — so nothing Stripe sends can produce one."""
        self.assertNotIn("enterprise", stripe_billing.PURCHASABLE_PLANS)
        for price in ("price_core_789", "price_pro_123", "price_max_456", "price_unknown"):
            self.assertNotEqual(stripe_billing.tier_for_price(price), "enterprise")
        summary = stripe_billing.handle_event(self.checkout_completed_event(plan="enterprise"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")
        self.assertIn("deferred", summary)

    # ---- teams -------------------------------------------------------- #

    def test_team_reconcile_keeps_a_comped_plan(self):
        self._pin_team("pro")
        with self._list_mock():
            summary = stripe_billing.reconcile_team_from_stripe(self.team)
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "pro")
        self.assertIn("unchanged", summary)
        # Members inherit through the effective tier, so the comp is
        # only really intact if this resolves.
        self.assertEqual(quota.get_effective_tier(self.user2.id), "pro")

    def test_team_reconcile_still_demotes_a_stripe_set_plan(self):
        self._pin_team("pro", source=TIER_SOURCE_STRIPE)
        with self._list_mock():
            summary = stripe_billing.reconcile_team_from_stripe(self.team)
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "free")
        self.assertEqual(summary, "team plan set to free")

    def test_team_subscription_deleted_keeps_a_comped_plan(self):
        self._pin_team("max")
        summary = stripe_billing.handle_event(
            {
                "id": "evt_td",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": "cus_team_1"}},
            }
        )
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "max")
        self.assertIn("unchanged", summary)

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
    "PRICE_PRO": "price_pro_123",
    "PRICE_MAX": "price_max_456",
    "AUTOMATIC_TAX": False,
}

STRIPE_DISABLED_SETTINGS = {
    "SECRET_KEY": "",
    "WEBHOOK_SECRET": "",
    "PRICE_PRO": "",
    "PRICE_MAX": "",
    "AUTOMATIC_TAX": False,
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
        self.assertEqual(res.data["plans"], ["pro", "max"])

    def test_enterprise_never_purchasable(self):
        self.assertNotIn("enterprise", stripe_billing.PURCHASABLE_PLANS)

    def test_partial_price_config_limits_plans(self):
        with override_settings(STRIPE={**STRIPE_TEST_SETTINGS, "PRICE_MAX": ""}):
            res = self.client.get(CONFIG_URL)
            self.assertEqual(res.data["plans"], ["pro"])

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
        alive = stripe.Customer.construct_from(
            {"id": "cus_abc", "object": "customer"}, "sk_test_x"
        )
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

        with mock.patch.object(
            stripe_billing, "reconcile_from_stripe", side_effect=fake_reconcile
        ):
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
        res = self.client.post(
            TEAM_PORTAL_URL, {"team_id": str(self.team.team_id)}, format="json"
        )
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
        self.assertEqual(list(tiers), ["free", "pro", "max", "enterprise"])
        quotas = dj_settings.SEARCH_ENGINE["TIER_QUOTAS"]
        for name, t in tiers.items():
            self.assertEqual(t["limits"]["llm_ask_daily"], quotas[name]["llm_ask_daily"])
            self.assertEqual(
                t["limits"]["message_retention_days"], quotas[name]["message_retention_days"]
            )
        self.assertNotIn("model_daily", tiers["pro"]["limits"])

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
        self.assertEqual(len(tiers), 4)
        self.assertIsNone(tiers["pro"]["price"])
        self.assertFalse(tiers["pro"]["purchasable"])

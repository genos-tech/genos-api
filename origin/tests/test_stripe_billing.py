"""Tests for the Stripe billing layer (service + endpoints + webhook).

Stripe's SDK is mocked at the service seam — `handle_event` operates
on plain dicts (what `construct_event` yields), and the views are
tested with the service functions patched, so no network and no real
keys. The tier writes are asserted against the DB, including the
effective-tier cache eviction.
"""

from unittest import mock

from django.test import override_settings

from origin.search_engine import quota
from origin.services import stripe_billing

from .test_base import BaseAPITestCase

CONFIG_URL = "/api/v2/billing/config/"
CHECKOUT_URL = "/api/v2/billing/checkout/"
PORTAL_URL = "/api/v2/billing/portal/"
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

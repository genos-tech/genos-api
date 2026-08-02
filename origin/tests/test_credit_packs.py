"""Buying AI credits — Stripe one-off purchases.

The failures here cost real money in both directions, so they are what
the tests are built around:

  1. **Granting twice.** Stripe delivers at least once, and the existing
     webhook has no dedup table — idempotency there is *convergence*
     (setting a tier is an assignment, so a replay is harmless). That
     reasoning does not survive an additive grant, so uniqueness has to
     come from the database.
  2. **Granting a subscription.** `handle_event` reads `metadata.plan`
     off a completed session and sets that tier outright. A one-off
     purchase must never be able to reach it.
  3. **Granting for money that never arrives.** A `mode=payment` session
     can complete `unpaid` — the ordinary shape for konbini and bank
     transfer, and JPY is the default currency.
  4. **Selling something inert** — a pack to an unlimited plan, or on a
     deployment where credits are not the enforced limit.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import override_settings

from origin.search_engine import credit_ledger
from origin.search_engine.models import AiCreditEntry
from origin.services import stripe_billing
from origin.tests.test_base import BaseAPITestCase

PACKS_URL = "/api/v2/billing/credit-packs/"
CHECKOUT_URL = "/api/v2/billing/credit-packs/checkout/"

PACK_STRIPE = {
    "SECRET_KEY": "sk_test_x",
    "WEBHOOK_SECRET": "whsec_x",
    "DEFAULT_CURRENCY": "usd",
    "PRICE_CORE": "price_core_1",
    "PRICE_PRO": "price_pro_1",
    "PRICE_MAX": "price_max_1",
    "PRICES_BY_CURRENCY": {},
    "CREDIT_PACK_PRICES": {
        "usd": {
            "pack_100": "price_pack100_usd",
            "pack_50": "price_pack50_usd",
            "pack_10": "price_pack10_usd",
        },
        "jpy": {"pack_100": "price_pack100_jpy"},
    },
    "PRICE_PRO_LEGACY": "",
    "PRICE_MAX_LEGACY": "",
    "AUTOMATIC_TAX": False,
    "TOS_CONSENT": False,
}


def _se(**over):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(over)
    return cfg


CREDITS_ON = override_settings(
    SEARCH_ENGINE=_se(AI_CREDITS_AUTHORITATIVE=True, AI_CREDITS_SHADOW=True)
)


@override_settings(STRIPE=PACK_STRIPE)
class PackTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.authenticate(self.user)

    def session(self, *, pack="pack_100", paid=True, user=None, extra_metadata=None):
        """A `mode=payment` Checkout Session, as Stripe sends it."""
        metadata = {"genos_user_id": str((user or self.user).id), "genos_credit_pack": pack}
        metadata.update(extra_metadata or {})
        return {
            "id": "cs_test_pack_1",
            "mode": "payment",
            "status": "complete",
            "payment_status": "paid" if paid else "unpaid",
            "client_reference_id": str((user or self.user).id),
            "customer": "cus_pack",
            "metadata": metadata,
        }

    def event(self, session, *, etype="checkout.session.completed"):
        return {"id": "evt_pack_1", "type": etype, "data": {"object": session}}

    def purchased_milli(self, user=None):
        return sum(
            e.credits_milli
            for e in AiCreditEntry.objects.filter(
                user_id=str((user or self.user).id),
                kind=AiCreditEntry.KIND_PURCHASED,
            )
        )


class GrantingTests(PackTestBase):
    def test_a_completed_pack_session_grants_the_credits(self):
        summary = stripe_billing.handle_event(self.event(self.session()))
        self.assertIn("granted", summary)
        self.assertEqual(self.purchased_milli(), 100_000)

    def test_a_redelivered_event_grants_once(self):
        """The one that would cost real money. Stripe retries; without
        the DB constraint the customer gets two packs for one payment."""
        stripe_billing.handle_event(self.event(self.session()))
        second = stripe_billing.handle_event(self.event(self.session()))

        self.assertIn("already granted", second)
        self.assertEqual(self.purchased_milli(), 100_000, "exactly one pack")
        self.assertEqual(AiCreditEntry.objects.filter(kind=AiCreditEntry.KIND_PURCHASED).count(), 1)

    def test_the_amount_comes_from_the_policy_not_the_event(self):
        """Metadata rides through the buyer's browser. The credits must
        be looked up server-side from the pack id alone."""
        session = self.session(extra_metadata={"credits": "999999"})
        stripe_billing.handle_event(self.event(session))
        self.assertEqual(self.purchased_milli(), 100_000)

    def test_an_unknown_pack_grants_nothing(self):
        summary = stripe_billing.handle_event(self.event(self.session(pack="pack_9999")))
        self.assertIn("ignored", summary)
        self.assertEqual(self.purchased_milli(), 0)


class TierSafetyTests(PackTestBase):
    def test_a_pack_never_changes_the_subscription_tier(self):
        """`handle_event` sets the tier from `metadata.plan` on a
        completed session. A pack carrying a stray one must not reach
        that arm — hence the branch on `mode` before everything else."""
        before = self.user.tier or "free"
        session = self.session(extra_metadata={"plan": "max"})
        summary = stripe_billing.handle_event(self.event(session))

        self.user.refresh_from_db()
        self.assertEqual(self.user.tier or "free", before, "the tier must be untouched")
        self.assertIn("granted", summary, "and the pack still lands")
        self.assertEqual(self.purchased_milli(), 100_000)

    def test_a_subscription_session_is_unaffected_by_the_new_branch(self):
        subscription_session = {
            "id": "cs_sub_1",
            "mode": "subscription",
            "client_reference_id": str(self.user.id),
            "customer": "cus_sub",
            "metadata": {"plan": "pro"},
        }
        stripe_billing.handle_event(self.event(subscription_session))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertEqual(self.purchased_milli(), 0, "and no credits were granted")


class DelayedPaymentTests(PackTestBase):
    """Checkout is card-only, so none of these SHOULD occur — they are
    the defense in depth for the day the method list is widened, when
    the failure would otherwise be paying and silently never receiving."""

    def test_an_unpaid_completed_session_grants_nothing_yet(self):
        summary = stripe_billing.handle_event(self.event(self.session(paid=False)))
        self.assertIn("deferred", summary)
        self.assertEqual(self.purchased_milli(), 0)

    def test_the_later_success_event_grants_once(self):
        stripe_billing.handle_event(self.event(self.session(paid=False)))
        summary = stripe_billing.handle_event(
            self.event(self.session(paid=True), etype="checkout.session.async_payment_succeeded")
        )
        self.assertIn("granted", summary)
        self.assertEqual(self.purchased_milli(), 100_000)

    def test_both_events_arriving_still_grants_once(self):
        # Stripe can send `completed` and `async_payment_succeeded` for
        # the same session; both key on the same id.
        stripe_billing.handle_event(
            self.event(self.session(paid=True), etype="checkout.session.async_payment_succeeded")
        )
        stripe_billing.handle_event(self.event(self.session(paid=True)))
        self.assertEqual(self.purchased_milli(), 100_000)


class PriceIsolationTests(PackTestBase):
    def test_a_pack_price_never_resolves_to_a_subscription_tier(self):
        """The reason pack prices live in their own settings dict.
        `tier_for_price` scans the PLAN maps; a pack id reachable from
        there would make buying credits grant a subscription."""
        for currency, packs in PACK_STRIPE["CREDIT_PACK_PRICES"].items():
            for pack, price_id in packs.items():
                self.assertIsNone(
                    stripe_billing.tier_for_price(price_id),
                    f"{currency}/{pack} price resolves to a tier",
                )

    def test_a_pack_is_only_sold_where_it_has_a_price(self):
        self.assertEqual(
            stripe_billing.purchasable_credit_packs("jpy"),
            ["pack_100"],
            "jpy has one configured price, so it offers one pack",
        )
        self.assertEqual(
            stripe_billing.purchasable_credit_packs("usd"),
            ["pack_100", "pack_50", "pack_10"],
            "largest first",
        )


class CheckoutEndpointTests(PackTestBase):
    def _stub_customer(self):
        """`ensure_customer` reaches Stripe before a session is created."""
        import stripe

        return mock.patch(
            "stripe.Customer.retrieve",
            return_value=stripe.Customer.construct_from(
                {"id": "cus_pack", "object": "customer"}, "sk_test_x"
            ),
        )

    @CREDITS_ON
    def test_checkout_returns_a_stripe_url(self):
        self.user.stripe_customer_id = "cus_pack"
        self.user.save(update_fields=["stripe_customer_id"])
        with (
            self._stub_customer(),
            mock.patch("stripe.checkout.Session.create") as create,
        ):
            create.return_value = {"url": "https://checkout.stripe.test/x"}
            res = self.client.post(CHECKOUT_URL, {"pack": "pack_50"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["url"], "https://checkout.stripe.test/x")

        params = create.call_args.kwargs
        self.assertEqual(params["mode"], "payment", "a pack must never recur")
        self.assertEqual(
            params["payment_method_types"],
            ["card"],
            "card only, by decision — konbini/bank transfer settle days "
            "later, and a delayed method here means paying and receiving "
            "nothing until an async event arrives",
        )
        self.assertNotIn("plan", params["metadata"], "a `plan` key would grant a subscription tier")
        self.assertEqual(params["metadata"]["genos_credit_pack"], "pack_50")

    @CREDITS_ON
    def test_an_unknown_pack_is_refused(self):
        res = self.client.post(CHECKOUT_URL, {"pack": "pack_nope"}, format="json")
        self.assertIn(res.status_code, (400, 503))

    @override_settings(SEARCH_ENGINE=_se(AI_CREDITS_AUTHORITATIVE=False))
    def test_packs_are_not_sold_when_credits_are_not_enforced(self):
        """In shadow mode the balance binds nobody, so a pack buys
        literally nothing."""
        res = self.client.post(CHECKOUT_URL, {"pack": "pack_100"}, format="json")
        self.assertEqual(res.status_code, 400)
        listing = self.client.get(PACKS_URL)
        self.assertFalse(listing.data["available"])

    @CREDITS_ON
    def test_packs_are_not_sold_to_an_unlimited_plan(self):
        """`balance_breakdown` short-circuits for an unlimited plan
        before it reads the ledger, so purchased credits would be
        invisible and unspendable."""
        self.user.tier = "enterprise"
        self.user.save(update_fields=["tier"])
        from origin.search_engine.quota import invalidate_effective_tier

        invalidate_effective_tier([self.user.id])
        self.addCleanup(invalidate_effective_tier, [self.user.id])

        res = self.client.post(CHECKOUT_URL, {"pack": "pack_100"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("unlimited", res.data["error"].lower())

    @CREDITS_ON
    def test_the_catalogue_lists_what_is_on_sale(self):
        import stripe

        price = stripe.Price.construct_from(
            {"id": "price_pack100_usd", "unit_amount": 3000, "currency": "usd"}, "sk_test_x"
        )
        with mock.patch("stripe.Price.retrieve", return_value=price):
            res = self.client.get(PACKS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["available"])
        by_pack = {p["pack"]: p for p in res.data["packs"]}
        self.assertEqual(by_pack["pack_100"]["credits"], 100)
        self.assertEqual(by_pack["pack_10"]["credits"], 10)
        self.assertNotIn(
            "interval",
            by_pack["pack_100"]["price"],
            "a one-off must not carry a recurring interval",
        )


class ReconcileTests(PackTestBase):
    """The repair path for a webhook that never arrived.

    There is none for one-time payments otherwise: `reconcile_from_stripe`
    lists Subscriptions only, so a dropped event would leave someone who
    paid with nothing to show for it and no way to fix it themselves.
    """

    def _sessions(self, *sessions):
        import stripe

        return stripe.ListObject.construct_from(
            {"object": "list", "data": list(sessions)}, "sk_test_x"
        )

    def test_it_grants_a_pack_the_webhook_never_delivered(self):
        self.user.stripe_customer_id = "cus_pack"
        self.user.save(update_fields=["stripe_customer_id"])
        with mock.patch(
            "stripe.checkout.Session.list", return_value=self._sessions(self.session())
        ):
            granted = stripe_billing.reconcile_credit_packs(self.user)
        self.assertEqual(granted, 1)
        self.assertEqual(self.purchased_milli(), 100_000)

    def test_it_is_a_no_op_once_the_webhook_has_landed(self):
        self.user.stripe_customer_id = "cus_pack"
        self.user.save(update_fields=["stripe_customer_id"])
        stripe_billing.handle_event(self.event(self.session()))

        with mock.patch(
            "stripe.checkout.Session.list", return_value=self._sessions(self.session())
        ):
            granted = stripe_billing.reconcile_credit_packs(self.user)
        self.assertEqual(granted, 0, "the session was already granted")
        self.assertEqual(self.purchased_milli(), 100_000, "and it stayed one pack")

    def test_it_ignores_subscription_sessions(self):
        self.user.stripe_customer_id = "cus_pack"
        self.user.save(update_fields=["stripe_customer_id"])
        subscription = {
            "id": "cs_sub_9",
            "mode": "subscription",
            "status": "complete",
            "payment_status": "paid",
            "client_reference_id": str(self.user.id),
            "customer": "cus_pack",
            "metadata": {"plan": "pro"},
        }
        with mock.patch("stripe.checkout.Session.list", return_value=self._sessions(subscription)):
            granted = stripe_billing.reconcile_credit_packs(self.user)
        self.assertEqual(granted, 0)
        self.assertEqual(self.purchased_milli(), 0)


class AbsorptionTests(PackTestBase):
    """Buying 100 has to give 100, even mid-month after an overshoot."""

    @CREDITS_ON
    def test_a_pack_bought_after_overshooting_this_month_arrives_whole(self):
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        credit_ledger.post_charge(
            request_id="11111111-1111-1111-1111-111111111111",
            user_id=str(self.user.id),
            credits_milli=8_000,  # 3 over free's 5
        )
        cache.clear()

        stripe_billing.handle_event(self.event(self.session(pack="pack_10")))
        cache.clear()

        breakdown = credit_ledger.balance_breakdown(str(self.user.id), "free")
        self.assertEqual(
            breakdown.purchased_milli,
            10_000,
            "they paid for 10 credits and must hold 10",
        )

    @CREDITS_ON
    def test_a_repeat_buyer_is_not_handed_back_what_they_spent(self):
        """The absorption must not become a gift: someone who genuinely
        spent an earlier pack carries that as debt, and buying again must
        not refund it."""
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        credit_ledger.post_purchase(
            user_id=str(self.user.id), credits_milli=10_000, external_ref="cs_first"
        )
        credit_ledger.post_charge(
            request_id="22222222-2222-2222-2222-222222222222",
            user_id=str(self.user.id),
            credits_milli=13_000,  # 5 monthly + 8 from the pack
        )
        cache.clear()

        credit_ledger.post_purchase(
            user_id=str(self.user.id), credits_milli=10_000, external_ref="cs_second"
        )
        cache.clear()

        breakdown = credit_ledger.balance_breakdown(str(self.user.id), "free")
        self.assertEqual(
            breakdown.purchased_milli,
            12_000,
            "20 bought, 8 legitimately spent from the first pack",
        )

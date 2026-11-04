"""MIP-996: a body that is not a JSON object is a 400 on the billing views, not a 500.

A billing request that got further would call Stripe: open a portal session, mint a
customer, start a checkout. So each test below checks two things for every body that is not
an object: the answer, and that nothing moved. The whole `_stripe()` boundary is a mock, so
ANY call to Stripe shows, and no user, team or credit row may change.

Two rules, picked per view (see `test_chat_non_object_body` for the same split):

* A view with a REQUIRED field reads its body through `body_object`: a body that is not an
  object reads as `{}` and gets the view's own answer for a missing field (checkout, team
  checkout, team portal).
* A view where an EMPTY body is a request in its own right refuses a non-object with 400
  "Request body must be a JSON object." (the personal portal: `{}` opens the portal home,
  so `[1]` must not; credit-pack checkout, whose missing-pack answer is a 503).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.models import AiCreditEntry
from origin.services import stripe_billing
from origin.views.common import billing_views

from .test_base import BaseAPITestCase
from .test_credit_packs import CREDITS_ON, PACK_STRIPE

User = get_user_model()

# JSON text, posted as it is. None of these is an object.
NOT_OBJECTS = ("[1]", '"abc"', "5", "true", "1.5", '[{"plan": "pro"}]')
# Empty-ish: they read as "no body" and were already an answer.
EMPTY_ISH = ("null", "[]", "0", '""', "false", "{}", "")

NOT_AN_OBJECT = {"error": "Request body must be a JSON object."}

CHECKOUT = "/api/v2/billing/checkout/"
PACK_CHECKOUT = "/api/v2/billing/credit-packs/checkout/"
PORTAL = "/api/v2/billing/portal/"
TEAM_CHECKOUT = "/api/v2/billing/team/checkout/"
TEAM_PORTAL = "/api/v2/billing/team/portal/"


@override_settings(STRIPE=PACK_STRIPE)
class _BillingClient(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The real 500 response if there is one, not the exception re-raised into the test.
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()
        # `_stripe()` is the one door to Stripe: every call (a customer, a checkout session, a
        # portal session, a subscription list) goes through what it returns, so a mock here sees
        # them all, and nothing can reach the network. Neither account has a customer id, so a
        # request that got through to Checkout would have to mint one.
        patcher = mock.patch.object(stripe_billing, "_stripe")
        self.stripe = patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, url, raw):
        return self.client.generic("POST", url, raw, content_type="application/json")

    def state(self):
        tables = (User, TeamMaster, TeamMembers, AiCreditEntry)
        return {m.__name__: list(m.objects.order_by("pk").values()) for m in tables}

    def assertNothingMoved(self, before, msg=None):
        self.assertEqual(self.stripe.mock_calls, [], msg or "Stripe was called")
        self.assertEqual(self.state(), before, msg or "a row changed")


class _MissingFieldAnswers:
    """A view with a required field: a body that is not an object answers as `{}` does."""

    URL: str
    MISSING_FIELD: str  # the view's answer to `{}`, in its own words

    def test_a_body_that_is_not_an_object_answers_as_an_empty_one_does(self):
        before = self.state()
        empty = self.send(self.URL, "{}")
        self.assertEqual((empty.status_code, empty.json()), (400, {"error": self.MISSING_FIELD}))

        for raw in NOT_OBJECTS:
            resp = self.send(self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, empty.json()), raw)
        self.assertNothingMoved(before)

    def test_an_empty_body_keeps_the_answer_it_always_had(self):
        for raw in EMPTY_ISH:
            resp = self.send(self.URL, raw)
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": self.MISSING_FIELD}), repr(raw)
            )


class CheckoutBodyShapeTests(_MissingFieldAnswers, _BillingClient):
    URL = CHECKOUT
    MISSING_FIELD = "Unknown plan ''."

    def test_a_valid_body_still_answers_what_it_did(self):
        with mock.patch.object(
            stripe_billing, "create_checkout_session", return_value="https://stripe/cs_1"
        ) as create:
            resp = self.send(self.URL, '{"plan": "max"}')
        self.assertEqual((resp.status_code, resp.json()), (200, {"url": "https://stripe/cs_1"}))
        self.assertEqual(create.call_args.args[1], "max")


class TeamCheckoutBodyShapeTests(_MissingFieldAnswers, _BillingClient):
    URL = TEAM_CHECKOUT
    MISSING_FIELD = "Unknown plan ''."

    def test_a_refused_body_mints_no_customer_for_the_team(self):
        for raw in NOT_OBJECTS:
            self.send(self.URL, raw)
        self.team.refresh_from_db()
        self.assertIsNone(self.team.stripe_customer_id)
        self.assertEqual(self.stripe.mock_calls, [])

    def test_a_valid_body_still_answers_what_it_did(self):
        with mock.patch.object(
            stripe_billing, "create_team_checkout_session", return_value="https://stripe/cs_t"
        ) as create:
            body = f'{{"team_id": "{self.team.team_id}", "plan": "max"}}'
            resp = self.send(self.URL, body)
        self.assertEqual((resp.status_code, resp.json()), (200, {"url": "https://stripe/cs_t"}))
        self.assertEqual(create.call_args.args[1], "max")


class TeamPortalBodyShapeTests(_MissingFieldAnswers, _BillingClient):
    URL = TEAM_PORTAL
    MISSING_FIELD = "team_id is required."

    def setUp(self):
        super().setUp()
        self.team.stripe_customer_id = "cus_team"
        self.team.save(update_fields=["stripe_customer_id"])
        self.stripe.return_value.billing_portal.Session.create.return_value = {
            "url": "https://stripe/p"
        }

    def test_a_valid_body_still_answers_what_it_did(self):
        resp = self.send(self.URL, f'{{"team_id": "{self.team.team_id}"}}')
        self.assertEqual((resp.status_code, resp.json()), (200, {"url": "https://stripe/p"}))
        self.stripe.return_value.billing_portal.Session.create.assert_called_once()

    def test_a_flow_or_plan_that_is_not_text_is_refused(self):
        before = self.state()
        for field in ("flow", "plan"):
            for value in ("5", "true", '["x"]', '{"a": 1}'):
                raw = f'{{"team_id": "{self.team.team_id}", "{field}": {value}}}'
                resp = self.send(self.URL, raw)
                self.assertEqual(
                    (resp.status_code, resp.json()),
                    (400, {"error": f"{field} must be a string."}),
                    raw,
                )
        self.assertNothingMoved(before)


@CREDITS_ON
class CreditPackCheckoutBodyShapeTests(_BillingClient):
    """`{}` is a 503 here ("Unknown credit pack ''."), which would put a client's mistake on
    the server-error counters, so a body that is not an object is a 400, not read as `{}`."""

    URL = PACK_CHECKOUT

    def test_a_body_that_is_not_an_object_is_refused_and_nothing_is_called(self):
        before = self.state()
        for raw in NOT_OBJECTS:
            resp = self.send(self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertNothingMoved(before)

    def test_an_empty_body_keeps_the_answer_it_always_had(self):
        for raw in EMPTY_ISH:
            resp = self.send(self.URL, raw)
            self.assertEqual(resp.status_code, 503, repr(raw))
            self.assertEqual(resp.json()["error"], "Unknown credit pack ''.", repr(raw))

    def test_the_refusal_comes_ahead_of_the_credits_gate(self):
        # Credits off: `{}` is told so, and so is a body naming a pack that is not text.
        not_enforced = {"error": "AI credits are not currently enforced on this deployment."}
        with mock.patch.object(billing_views, "_credit_packs_available_for") as gate:
            gate.return_value = (False, not_enforced["error"])
            for raw in ("{}", '{"pack": [1]}', '{"pack": 5}'):
                resp = self.send(self.URL, raw)
                self.assertEqual((resp.status_code, resp.json()), (400, not_enforced), raw)
            # A body that is not an object never gets as far as asking.
            gate.reset_mock()
            resp = self.send(self.URL, "[1]")
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT))
            gate.assert_not_called()

    def test_a_pack_that_is_not_text_is_refused_and_nothing_is_called(self):
        before = self.state()
        for value in ("5", "true", "1.5", "[1]", '{"a": 1}'):
            resp = self.send(self.URL, f'{{"pack": {value}}}')
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": "pack must be a string."}), value
            )
        self.assertNothingMoved(before)

    def test_a_valid_body_still_answers_what_it_did(self):
        with mock.patch.object(
            stripe_billing,
            "create_credit_pack_checkout_session",
            return_value="https://stripe/cs_p",
        ) as create:
            resp = self.send(self.URL, '{"pack": "pack_50"}')
        self.assertEqual((resp.status_code, resp.json()), (200, {"url": "https://stripe/cs_p"}))
        self.assertEqual(create.call_args.args[1:], ("pack_50", "usd"))

    def test_the_currency_is_still_read_from_the_body(self):
        with (
            mock.patch.object(stripe_billing, "supported_currencies", return_value=["usd", "jpy"]),
            mock.patch.object(
                stripe_billing, "create_credit_pack_checkout_session", return_value="u"
            ) as create,
        ):
            self.send(self.URL, '{"pack": "pack_50", "currency": "JPY"}')
        self.assertEqual(create.call_args.args[1:], ("pack_50", "jpy"))


class RequestedCurrencyTests(BaseAPITestCase):
    """`_requested_currency` reads `currency` off a POST body. Every POST view that calls it
    has refused a body that is not an object by then, so no request reaches it with one; it
    is pinned directly so it stays safe for the next view that does."""

    def test_a_body_that_is_not_an_object_reads_as_the_default_currency(self):
        default = stripe_billing.default_currency()
        for data in ([1], "abc", 5, True, 1.5, [{"currency": "jpy"}]):
            request = SimpleNamespace(method="POST", data=data)
            self.assertEqual(billing_views._requested_currency(request), default, repr(data))

    def test_an_object_still_names_its_currency(self):
        with mock.patch.object(stripe_billing, "supported_currencies", return_value=["usd", "jpy"]):
            request = SimpleNamespace(method="POST", data={"currency": " JPY "})
            self.assertEqual(billing_views._requested_currency(request), "jpy")


class PortalBodyShapeTests(_BillingClient):
    """`{}` opens the portal home, so `[1]` must not: a Stripe portal session is a real call."""

    URL = PORTAL

    def setUp(self):
        super().setUp()
        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])
        self.session_create = self.stripe.return_value.billing_portal.Session.create
        self.session_create.return_value = {"url": "https://stripe/p"}

    def test_a_body_that_is_not_an_object_is_refused_and_opens_no_session(self):
        before = self.state()
        for raw in NOT_OBJECTS:
            resp = self.send(self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)
        self.assertNothingMoved(before)
        self.session_create.assert_not_called()

    def test_an_empty_body_still_opens_the_portal_home(self):
        # The control for the test above: this is what `[1]` would have done.
        for raw in EMPTY_ISH:
            self.session_create.reset_mock()
            resp = self.send(self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (200, {"url": "https://stripe/p"}))
            self.session_create.assert_called_once()

    def test_a_flow_or_plan_that_is_not_text_is_refused_and_opens_no_session(self):
        before = self.state()
        for field in ("flow", "plan"):
            for value in ("5", "true", "1.5", '["x"]', '{"a": 1}'):
                resp = self.send(self.URL, f'{{"{field}": {value}}}')
                self.assertEqual(
                    (resp.status_code, resp.json()),
                    (400, {"error": f"{field} must be a string."}),
                    (field, value),
                )
        self.assertNothingMoved(before)

    def test_the_answers_to_a_flow_that_is_text_are_unchanged(self):
        resp = self.send(self.URL, '{"flow": "bogus"}')
        self.assertEqual(
            (resp.status_code, resp.json()), (400, {"error": "Unknown portal flow 'bogus'."})
        )
        resp = self.send(self.URL, '{"flow": "update"}')
        self.assertEqual((resp.status_code, resp.json()), (400, {"error": "Unknown plan None."}))
        # An empty flow or plan is "not sent": the portal home.
        resp = self.send(self.URL, '{"flow": 0, "plan": false}')
        self.assertEqual(resp.status_code, 200)

    def test_flow_is_judged_before_plan_as_it_was(self):
        resp = self.send(self.URL, '{"flow": "bogus", "plan": 5}')
        self.assertEqual(
            (resp.status_code, resp.json()), (400, {"error": "Unknown portal flow 'bogus'."})
        )

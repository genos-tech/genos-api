"""`manage.py feature_access set-tier` / `set-team-plan` — the operator
path, and the provenance it now writes.

A tier set here is a comp, a trial, or a support gesture: Stripe knows
nothing about it, and "Stripe knows nothing" is precisely what
`reconcile_from_stripe` otherwise reads as "lapsed → free". The command
is therefore the only writer of `tier_source='operator'`, which makes it
the only thing standing between a comped account and a silent demotion
on its owner's next return from checkout.

The service-side half of the rule (a real subscription overrides the pin
and hands the account back to billing) lives in
`test_stripe_billing.TierProvenanceTests`.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from origin.models.common.user_models import TIER_SOURCE_OPERATOR, TIER_SOURCE_STRIPE
from origin.search_engine import quota
from origin.services import stripe_billing

from .test_base import BaseAPITestCase
from .test_stripe_billing import STRIPE_TEST_SETTINGS


class FeatureAccessTierTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()

    def _set_tier(self, *args):
        out = StringIO()
        call_command("feature_access", "set-tier", "--email", self.user.email, *args, stdout=out)
        self.user.refresh_from_db()
        return out.getvalue()

    def _set_team_plan(self, *args):
        out = StringIO()
        call_command(
            "feature_access",
            "set-team-plan",
            "--team-id",
            str(self.team.team_id),
            *args,
            stdout=out,
        )
        self.team.refresh_from_db()
        return out.getvalue()

    # ---- personal ---------------------------------------------------- #

    def test_set_tier_pins_by_default(self):
        output = self._set_tier("--tier", "max")
        self.assertEqual(self.user.tier, "max")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_OPERATOR)
        self.assertIn("operator-set", output)
        self.assertEqual(quota.get_effective_tier(self.user.id), "max")

    def test_pinning_a_tier_that_is_already_correct(self):
        """The repair an operator actually runs: the account is already
        on the right tier and only needs the pin. The old
        `previous == new_tier` early return made this a silent no-op —
        which is exactly how an account stays demotable after someone
        has "fixed" it."""
        self.user.tier = "max"
        self.user.save(update_fields=["tier"])

        output = self._set_tier("--tier", "max")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_OPERATOR)
        # And it must SAY so, or the operator can't tell it landed.
        self.assertIn("unchanged", output)
        self.assertIn("operator-set", output)
        self.assertNotIn("No change", output)

    def test_source_stripe_hands_the_account_back(self):
        self._set_tier("--tier", "pro")
        output = self._set_tier("--tier", "pro", "--source", "stripe")
        self.assertEqual(self.user.tier_source, TIER_SOURCE_STRIPE)
        self.assertIn("stripe-set", output)

    def test_a_true_no_op_says_no_change(self):
        self._set_tier("--tier", "pro")
        output = self._set_tier("--tier", "pro")
        self.assertIn("No change", output)

    def test_a_note_is_echoed(self):
        output = self._set_tier("--tier", "core", "--note", "hackathon winner")
        self.assertIn("hackathon winner", output)

    @override_settings(STRIPE=STRIPE_TEST_SETTINGS)
    def test_a_comp_survives_the_reconcile(self):
        """End to end, the way it actually goes wrong: comp the account,
        then let it come back from Stripe with a customer id and no
        subscription — the shape a credit-pack purchase leaves behind."""
        import stripe  # noqa: PLC0415

        self._set_tier("--tier", "max")
        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])

        empty = stripe.ListObject.construct_from(
            {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"},
            "sk_test_x",
        )
        with mock.patch("stripe.Subscription.list", return_value=empty):
            stripe_billing.reconcile_from_stripe(self.user)

        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "max")

    # ---- teams -------------------------------------------------------- #

    def test_set_team_plan_pins_by_default(self):
        output = self._set_team_plan("--plan", "pro")
        self.assertEqual(self.team.plan, "pro")
        self.assertEqual(self.team.plan_source, TIER_SOURCE_OPERATOR)
        self.assertIn("operator-set", output)
        # Members inherit the plan as their effective tier.
        self.assertEqual(quota.get_effective_tier(self.user2.id), "pro")

    def test_pinning_a_team_plan_that_is_already_correct(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        output = self._set_team_plan("--plan", "pro")
        self.assertEqual(self.team.plan_source, TIER_SOURCE_OPERATOR)
        self.assertIn("unchanged", output)
        self.assertNotIn("No change", output)

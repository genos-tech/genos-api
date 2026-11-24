"""A team that closes with its last member also stops billing (MIP-2285).

`remove_team_member` closed the team (`close_team_if_empty`) without any Stripe call: a paid team kept
billing and `team_purge` held it for billing for ever. Same class as MIP-2254 (account deletion), same
helpers, same fail-closed rule. Stripe is mocked at the `stripe_billing._stripe` seam.
"""

from unittest import mock

from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.services import stripe_billing, team_purge
from origin.services.team_membership import TeamBillingCancelFailed, remove_team_member

from .test_account_deletion_billing import BILLING_ON, FakeStripe, _StripeError, _sub
from .test_base import BaseAPITestCase


class TeamLeaveBillingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patch = override_settings(STRIPE={**_settings(), **BILLING_ON})
        patch.enable()
        self.addCleanup(patch.disable)
        self.fake = FakeStripe()
        seam = mock.patch.object(stripe_billing, "_stripe", lambda: self.fake)
        seam.start()
        self.addCleanup(seam.stop)
        self.owner = CustomUser.objects.create_user(
            username="lastone", email="lastone@example.com", password="lastpass123"
        )
        self.paid = TeamMaster.objects.create(team_name="paid", owner=self.owner)
        TeamMembers.objects.create(team=self.paid, attendee=self.owner)
        self.paid.stripe_customer_id = "cus_paid"
        self.paid.plan = "core"
        self.paid.plan_set_by = "stripe"
        self.paid.save()
        self.fake.subs["cus_paid"] = [_sub("sub_paid")]

    def test_the_last_member_leaving_cancels_the_subscription_and_ends_the_plan(self):
        remove_team_member(self.paid.team_id, self.owner.id)

        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_paid"])
        self.paid.refresh_from_db()
        self.assertTrue(self.paid.is_deleted)
        self.assertEqual(self.paid.plan, "free")
        self.assertFalse(team_purge._billing_hold(self.paid))

    def test_a_stripe_failure_refuses_the_leave_and_changes_nothing(self):
        self.fake.fail_cancel = _StripeError("api_connection_error")

        with self.assertRaises(TeamBillingCancelFailed):
            remove_team_member(self.paid.team_id, self.owner.id)

        self.paid.refresh_from_db()
        self.assertFalse(self.paid.is_deleted)
        self.assertEqual(self.paid.plan, "core")
        self.assertTrue(
            TeamMembers.objects.filter(
                team=self.paid, attendee=self.owner, is_deleted=False
            ).exists()
        )

    def test_the_leave_can_be_repeated_once_stripe_answers(self):
        self.fake.fail_cancel = _StripeError()
        with self.assertRaises(TeamBillingCancelFailed):
            remove_team_member(self.paid.team_id, self.owner.id)
        self.fake.fail_cancel = None

        remove_team_member(self.paid.team_id, self.owner.id)

        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_paid"])
        self.paid.refresh_from_db()
        self.assertTrue(self.paid.is_deleted)

    def test_a_team_that_still_has_a_live_member_is_untouched(self):
        TeamMembers.objects.create(team=self.paid, attendee=self.user2)

        remove_team_member(self.paid.team_id, self.owner.id)

        self.assertEqual(self.fake.cancelled, [])
        self.assertEqual(self.fake.listed, [])
        self.paid.refresh_from_db()
        self.assertFalse(self.paid.is_deleted)
        self.assertEqual(self.paid.plan, "core")

    def test_a_team_that_never_paid_makes_no_stripe_call(self):
        free = TeamMaster.objects.create(team_name="free", owner=self.owner)
        TeamMembers.objects.create(team=free, attendee=self.owner)

        remove_team_member(free.team_id, self.owner.id)

        self.assertEqual(self.fake.listed, [])
        free.refresh_from_db()
        self.assertTrue(free.is_deleted)

    def test_billing_switched_off_does_not_block_the_leave(self):
        with override_settings(STRIPE={**_settings(), "SECRET_KEY": ""}):
            remove_team_member(self.paid.team_id, self.owner.id)

        self.assertEqual(self.fake.listed, [])
        self.paid.refresh_from_db()
        self.assertTrue(self.paid.is_deleted)

    def test_the_leave_endpoint_answers_503_and_leaves_the_team_when_stripe_fails(self):
        # The owner cannot leave through the endpoint: make this person the last member of a team
        # whose owner is someone else's row, then have Stripe refuse.
        TeamMembers.objects.filter(team=self.paid, attendee=self.owner).update(is_deleted=True)
        TeamMembers.objects.create(team=self.paid, attendee=self.user2)
        self.fake.fail_cancel = _StripeError()
        self.authenticate(self.user2)

        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.paid.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )

        self.assertEqual(res.status_code, 503)
        self.paid.refresh_from_db()
        self.assertFalse(self.paid.is_deleted)
        self.assertTrue(
            TeamMembers.objects.filter(
                team=self.paid, attendee=self.user2, is_deleted=False
            ).exists()
        )

        self.fake.fail_cancel = None
        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.paid.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_paid"])


def _settings():
    from django.conf import settings

    return dict(settings.STRIPE)

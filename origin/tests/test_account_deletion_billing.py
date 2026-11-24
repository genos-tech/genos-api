"""Deleting an account stops its Stripe subscriptions (MIP-2254).

Before this, `delete_account` made no Stripe call: a person with a paid plan was erased and kept being
charged (reproduced in Stripe test mode on 2026-11-24, for a personal plan and for the plan of a solo team
that closed with them). Stripe is mocked at the module seam; the shapes it returns are the SDK's own
(`json.loads(str(resp))`, like the rest of `stripe_billing`).
"""

import json
from unittest import mock

from django.test import override_settings
from rest_framework import status

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.services import stripe_billing, team_purge
from origin.services.account_deletion import (
    BillingCancelFailed,
    OwnershipTransferRequired,
    delete_account,
)

from .test_base import BaseAPITestCase

URL = "/api/v2/user/account/"
BILLING_ON = {"SECRET_KEY": "sk_test_x", "WEBHOOK_SECRET": "whsec_x"}


class _Page(dict):
    """What `Subscription.list` returns: the code reads it through `str()` and `json.loads`."""

    def __str__(self):
        return json.dumps(self)


class _StripeError(Exception):
    def __init__(self, code=""):
        super().__init__("stripe says no")
        self.code = code


def _sub(sub_id, status_="active"):
    return {"id": sub_id, "status": status_}


class FakeStripe:
    """Stands in for the `stripe` module: subscriptions per customer, and every call recorded."""

    def __init__(self, subs_by_customer=None):
        self.subs = subs_by_customer or {}
        self.cancelled = []  # (subscription id, kwargs)
        self.listed = []  # customer ids asked about
        self.fail_cancel = None  # a _StripeError to raise on cancel
        self.fail_list = None
        outer = self

        class Subscription:
            @staticmethod
            def list(**params):
                outer.listed.append(params["customer"])
                if outer.fail_list:
                    raise outer.fail_list
                subs = outer.subs.get(params["customer"], [])
                start = 0
                if params.get("starting_after"):
                    start = [s["id"] for s in subs].index(params["starting_after"]) + 1
                size = params.get("limit", 100)
                page = subs[start : start + size]
                return _Page(data=page, has_more=start + size < len(subs))

            @staticmethod
            def cancel(sub_id, **kwargs):
                if outer.fail_cancel:
                    raise outer.fail_cancel
                outer.cancelled.append((sub_id, kwargs))

        self.Subscription = Subscription


class DeletionBillingBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        settings_patch = override_settings(STRIPE={**stripe_billing_settings(), **BILLING_ON})
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.fake = FakeStripe()
        seam = mock.patch.object(stripe_billing, "_stripe", lambda: self.fake)
        seam.start()
        self.addCleanup(seam.stop)
        # An extra person who owns a team alone, for the solo-team cases.
        self.solo = CustomUser.objects.create_user(
            username="solo", email="solo@example.com", password="solopass123"
        )
        self.solo_team = TeamMaster.objects.create(team_name="solo team", owner=self.solo)
        TeamMembers.objects.create(team=self.solo_team, attendee=self.solo)

    def bind(self, obj, customer_id, **fields):
        obj.stripe_customer_id = customer_id
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.save()


def stripe_billing_settings():
    from django.conf import settings

    return dict(settings.STRIPE)


class PersonalSubscriptionTests(DeletionBillingBase):
    def test_the_active_subscription_is_cancelled_at_once_without_proration(self):
        self.bind(self.user2, "cus_personal", tier="core")
        self.fake.subs["cus_personal"] = [_sub("sub_live"), _sub("sub_old", "canceled")]

        delete_account(self.user2)

        self.assertEqual(
            self.fake.cancelled, [("sub_live", {"invoice_now": False, "prorate": False})]
        )

    def test_every_billing_status_is_cancelled_and_only_ended_ones_are_skipped(self):
        self.bind(self.user2, "cus_personal")
        statuses = ["active", "trialing", "past_due", "unpaid", "paused", "incomplete"]
        ended = ["canceled", "incomplete_expired"]
        self.fake.subs["cus_personal"] = [_sub(f"sub_{s}", s) for s in statuses + ended]

        delete_account(self.user2)

        self.assertEqual({c[0] for c in self.fake.cancelled}, {f"sub_{s}" for s in statuses})

    def test_more_than_one_page_of_subscriptions_is_all_cancelled(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub(f"sub_{i}") for i in range(230)]

        delete_account(self.user2)

        self.assertEqual(len(self.fake.cancelled), 230)

    def test_a_person_who_never_paid_makes_no_stripe_call(self):
        with mock.patch.object(stripe_billing, "_stripe", side_effect=AssertionError("no call")):
            delete_account(self.user2)
        self.assertEqual(self.fake.listed, [])

    def test_the_customer_record_is_kept_on_the_row(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_live")]

        delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertEqual(self.user2.stripe_customer_id, "cus_personal")  # MIP-874
        self.assertTrue(self.user2.is_deleted)

    def test_a_customer_that_no_longer_exists_does_not_stop_the_deletion(self):
        self.bind(self.user2, "cus_gone")
        self.fake.fail_list = _StripeError("resource_missing")

        delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)

    def test_a_subscription_that_ended_in_between_counts_as_done(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_live")]
        self.fake.fail_cancel = _StripeError("resource_missing")

        delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)

    def test_billing_switched_off_does_not_block_the_deletion(self):
        self.bind(self.user2, "cus_personal")
        with override_settings(STRIPE={**stripe_billing_settings(), "SECRET_KEY": ""}):
            delete_account(self.user2)
        self.assertEqual(self.fake.listed, [])
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)


class FailClosedTests(DeletionBillingBase):
    def test_a_stripe_failure_stops_the_deletion_before_anything_is_erased(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_live")]
        self.fake.fail_cancel = _StripeError("api_connection_error")

        with self.assertRaises(BillingCancelFailed):
            delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_active)
        self.assertFalse(self.user2.is_deleted)
        self.assertEqual(self.user2.email, "other@example.com")
        self.assertTrue(TeamMembers.objects.filter(attendee=self.user2, is_deleted=False).exists())

    def test_a_list_failure_stops_it_too(self):
        self.bind(self.user2, "cus_personal")
        self.fake.fail_list = _StripeError("api_connection_error")
        with self.assertRaises(BillingCancelFailed):
            delete_account(self.user2)
        self.user2.refresh_from_db()
        self.assertFalse(self.user2.is_deleted)

    def test_the_deletion_can_be_repeated_once_stripe_answers(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_live")]
        self.fake.fail_cancel = _StripeError("api_connection_error")
        with self.assertRaises(BillingCancelFailed):
            delete_account(self.user2)

        self.fake.fail_cancel = None
        delete_account(self.user2)

        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_live"])
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)

    def test_the_endpoint_answers_503_and_leaves_the_account(self):
        self.bind(self.user2, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_live")]
        self.fake.fail_cancel = _StripeError("api_connection_error")
        self.client.force_authenticate(self.user2)

        resp = self.client.delete(
            URL, {"confirm": "DELETE", "password": "otherpass123"}, format="json"
        )

        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("stripe", json.dumps(resp.json()).lower())  # Stripe's text is never shown
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_active)

    def test_a_team_that_others_depend_on_still_blocks_and_nothing_is_cancelled(self):
        self.bind(self.user, "cus_owner")
        self.fake.subs["cus_owner"] = [_sub("sub_live")]

        with self.assertRaises(OwnershipTransferRequired):
            delete_account(self.user)

        self.assertEqual(self.fake.cancelled, [])
        self.assertEqual(self.fake.listed, [])


class TeamSubscriptionTests(DeletionBillingBase):
    def paid_solo_team(self):
        self.bind(self.solo_team, "cus_team", plan="core", plan_set_by="stripe")
        self.fake.subs["cus_team"] = [_sub("sub_team")]

    def test_the_subscription_of_a_team_that_dies_with_the_person_is_cancelled(self):
        self.paid_solo_team()

        delete_account(self.solo)

        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_team"])

    def test_the_closed_team_drops_to_free_so_the_purge_no_longer_holds_it(self):
        self.paid_solo_team()

        delete_account(self.solo)

        self.solo_team.refresh_from_db()
        self.assertTrue(self.solo_team.is_deleted)
        self.assertEqual(self.solo_team.plan, "free")
        self.assertFalse(team_purge._billing_hold(self.solo_team))
        self.assertIn(self.solo_team.team_id, team_purge.pending_purge_team_ids())

    def test_an_operator_set_plan_is_not_taken_away(self):
        self.bind(self.solo_team, "cus_team", plan="pro", plan_set_by="operator")
        self.fake.subs["cus_team"] = [_sub("sub_team")]

        delete_account(self.solo)

        self.solo_team.refresh_from_db()
        self.assertEqual(self.solo_team.plan, "pro")

    def test_the_last_member_of_a_team_someone_else_owned_closes_it_and_its_plan(self):
        # Owner long gone (deleted), this person is the last live member: the team closes with them.
        owner_gone = CustomUser.objects.create_user(
            username="gone", email="gone@example.com", password="gonepass123"
        )
        owner_gone.is_deleted = True
        owner_gone.save()
        orphan = TeamMaster.objects.create(team_name="orphan", owner=owner_gone)
        TeamMembers.objects.create(team=orphan, attendee=self.user2)
        self.bind(orphan, "cus_orphan", plan="core", plan_set_by="stripe")
        self.fake.subs["cus_orphan"] = [_sub("sub_orphan")]
        # self.user2 also sits in the shared team with self.user (alive), which must be untouched.
        self.bind(self.team, "cus_shared", plan="pro", plan_set_by="stripe")
        self.fake.subs["cus_shared"] = [_sub("sub_shared")]

        delete_account(self.user2)

        self.assertEqual([c[0] for c in self.fake.cancelled], ["sub_orphan"])
        orphan.refresh_from_db()
        self.assertEqual(orphan.plan, "free")
        self.team.refresh_from_db()
        self.assertEqual(self.team.plan, "pro")

    def test_a_team_the_person_only_belongs_to_keeps_its_subscription(self):
        self.bind(self.team, "cus_shared", plan="pro", plan_set_by="stripe")
        self.fake.subs["cus_shared"] = [_sub("sub_shared")]

        delete_account(self.user2)

        self.assertEqual(self.fake.cancelled, [])
        self.assertNotIn("cus_shared", self.fake.listed)

    def test_personal_and_team_subscriptions_are_both_cancelled(self):
        self.paid_solo_team()
        self.bind(self.solo, "cus_personal")
        self.fake.subs["cus_personal"] = [_sub("sub_personal")]

        delete_account(self.solo)

        self.assertEqual({c[0] for c in self.fake.cancelled}, {"sub_personal", "sub_team"})

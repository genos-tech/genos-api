"""The first month of Core, free (MIP-1737).

What is pinned, and why each is its own test:

  * The offer is OFF by default and off means "no new trial", while a running one keeps its days.
  * A trial is an overlay read by `resolve_effective_tier`; it never writes `CustomUser.tier` or
    `tier_set_by`, never touches Stripe, and ends by the clock with nothing written.
  * A payer, a comp, a paying team's member are untouched, and their one trial is not burned.
  * A Stripe webhook (subscription deleted / unpaid / a reconcile that finds nothing) cannot end
    a trial early, and subscribing during a trial simply works.
  * One per person while the account exists (one row per account); nothing is kept after the
    account is deleted, so a new sign-up may have a new first month (MIP-1779). Once for existing
    accounts, never back-dated.
  * At day 30 the plan reads as before, the data is all still there, a connected app is kept and
    shown as paused, and an MCP call is refused until the plan is back.
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from io import StringIO
from unittest import mock

import stripe
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.models import Application
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.trial_models import CoreTrial
from origin.models.common.user_models import TIER_SET_BY_OPERATOR, CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import quota
from origin.services import (
    account_deletion,
    first_month_trial,
    schedule_defaults,
    stripe_billing,
)
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_foundation import oauth_on
from origin.tests.test_stripe_billing import STRIPE_TEST_SETTINGS, BillingTestBase

FEATURES = "/api/v2/agent/features/"
CONNECTIONS = "/api/v2/mcp/connections/"
MCP = "/api/public/v1/mcp"


def offer(on: bool):
    """The switch, set the way production sets it (an environment variable)."""
    env = {k: v for k, v in os.environ.items() if k != first_month_trial.SWITCH_ENV}
    if on:
        env[first_month_trial.SWITCH_ENV] = "true"
    return mock.patch.dict(os.environ, env, clear=True)


def verified(user: CustomUser) -> CustomUser:
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    return user


class TrialBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        verified(self.user)
        verified(self.user2)
        cache.clear()
        self.addCleanup(cache.clear)

    def tier(self, user=None) -> str:
        user = user or self.user
        quota.invalidate_effective_tier([str(user.id)])
        return quota.get_effective_tier(str(user.id))

    def end_now(self, user=None):
        """Move the trial's end into the past: day 30 has arrived."""
        user = user or self.user
        CoreTrial.objects.filter(user=user).update(ends_at=timezone.now() - timedelta(seconds=1))
        quota.invalidate_effective_tier([str(user.id)])

    def start(self, user=None):
        with offer(True):
            return first_month_trial.start_for_new_account(user or self.user)


class StartAndSwitchTests(TrialBase):
    def test_the_offer_is_off_by_default(self):
        with offer(False):
            self.assertFalse(first_month_trial.offer_is_open())
            self.assertIsNone(first_month_trial.start_for_new_account(self.user))
        self.assertFalse(CoreTrial.objects.exists())
        self.assertEqual(self.tier(), "free")

    def test_only_a_clear_on_switches_it_on(self):
        for raw, expected in [
            ("true", True),
            ("1", True),
            (" ON ", True),
            ("", False),
            ("false", False),
            ("0", False),
            ("maybe", False),
        ]:
            with mock.patch.dict(os.environ, {first_month_trial.SWITCH_ENV: raw}):
                self.assertIs(first_month_trial.offer_is_open(), expected, raw)

    def test_a_new_verified_account_gets_thirty_days_of_core(self):
        before = timezone.now()
        trial = self.start()
        self.assertIsNotNone(trial)
        self.assertEqual(trial.ends_at - trial.started_at, timedelta(days=30))
        self.assertGreaterEqual(trial.started_at, before)
        self.assertEqual(self.tier(), "core")
        resolved = quota.resolve_effective_tier(str(self.user.id))
        self.assertEqual(resolved["trial_ends_at"], trial.ends_at.isoformat())

    def test_the_trial_gives_core_values_and_changes_no_number(self):
        before = {t: {**c} for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        self.assertEqual(self.tier(), "free")
        self.start()
        uid = str(self.user.id)
        core = settings.SEARCH_ENGINE["TIER_QUOTAS"]["core"]
        self.assertEqual(quota.get_agent_tool_level(uid), core["agent_tool_level"])
        self.assertEqual(quota.get_message_retention_days(uid), core["message_retention_days"])
        self.assertEqual(quota.get_quota(uid, quota.TASK_CREATE_KEY), core["task_create_monthly"])
        self.assertEqual(settings.SEARCH_ENGINE["TIER_QUOTAS"], before)

    def test_the_trial_writes_no_tier_and_touches_no_stripe(self):
        with mock.patch.object(stripe_billing, "_stripe", side_effect=AssertionError("Stripe")):
            self.start()
            self.tier()
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "free")
        self.assertEqual(self.user.tier_set_by, "stripe")
        self.assertEqual(self.user.stripe_customer_id or "", "")

    def test_the_features_payload_names_the_end_date_only_while_it_grants_the_plan(self):
        self.authenticate()
        self.assertIsNone(self.client.get(FEATURES).data["trial_ends_at"])
        trial = self.start()
        quota.invalidate_effective_tier([str(self.user.id)])
        data = self.client.get(FEATURES).data
        self.assertEqual(data["tier"], "core")
        self.assertEqual(data["tier_source"], "personal")
        self.assertEqual(data["trial_ends_at"], trial.ends_at.isoformat())
        self.end_now()
        data = self.client.get(FEATURES).data
        self.assertEqual(data["tier"], "free")
        self.assertIsNone(data["trial_ends_at"])

    def test_an_account_that_is_not_a_live_verified_person_gets_nothing(self):
        cases = {}
        for label, change in {
            "unverified": {"is_email_verified": False},
            "inactive": {"is_active": False},
            "deleted": {"is_deleted": True},
            "demo": {"is_demo": True},
            "system": {"is_system_user": True},
        }.items():
            user = CustomUser.objects.create_user(
                email=f"{label}@example.com", username=label, password="x"
            )
            verified(user)
            CustomUser.objects.filter(pk=user.pk).update(**change)
            user.refresh_from_db()
            cases[label] = user
        for label, user in cases.items():
            with self.subTest(label):
                # The REASON, not only "None": a crash would also return None.
                self.assertEqual(
                    first_month_trial.why_not(user), first_month_trial.NOT_ELIGIBLE_ACCOUNT
                )
                self.assertIsNone(self.start(user))
        self.assertFalse(CoreTrial.objects.exists())

    def test_an_account_with_no_real_address_gets_nothing(self):
        user = verified(
            CustomUser.objects.create_user(
                email="github_123@noemail.local", username="gh", password="x"
            )
        )
        self.assertEqual(first_month_trial.why_not(user), first_month_trial.NOT_ELIGIBLE_ACCOUNT)
        self.assertIsNone(self.start(user))

    def test_a_placeholder_address_starts_nothing_on_any_path_and_a_real_one_still_does(self):
        """MIP-1771: a GitHub sign-up with no public email is `github_<id>@noemail.local` and
        marked verified, and each such account has its own id, so the one-per-address rule cannot
        catch it. The hook and the grant command both refuse it, whatever the case; a normal
        verified address starts one as before, and a trial already running is left alone."""
        running = self.start()  # self.user: a real, verified address
        self.assertIsNotNone(running)
        placeholders = [
            verified(CustomUser.objects.create_user(email=email, username=name, password="x"))
            for email, name in [
                ("github_1@noemail.local", "gh1"),
                ("GitHub_2@NoEmail.Local", "gh2"),
                ("google_3@noemail.local", "go3"),
            ]
        ]
        for user in placeholders:
            self.assertEqual(
                first_month_trial.why_not(user), first_month_trial.NOT_ELIGIBLE_ACCOUNT
            )
            self.assertIsNone(self.start(user))
        with offer(True):
            summary = first_month_trial.grant_to_existing()
        self.assertEqual(
            summary.started, 1
        )  # self.user2, a real address (self.user already had one)
        for user in placeholders:
            self.assertFalse(CoreTrial.objects.filter(user=user).exists())
        self.assertEqual(CoreTrial.objects.get(user=self.user).ends_at, running.ends_at)
        self.assertTrue(CoreTrial.objects.filter(user=self.user2).exists())

    def test_the_second_call_finds_the_first_row(self):
        first = self.start()
        self.assertIsNone(self.start())
        self.assertEqual(CoreTrial.objects.filter(user=self.user).count(), 1)
        self.assertEqual(CoreTrial.objects.get(user=self.user).ends_at, first.ends_at)

    def test_a_stale_full_save_of_the_user_cannot_erase_or_restart_it(self):
        stale = CustomUser.objects.get(pk=self.user.pk)
        self.start()
        stale.save()  # writes every column of the user row
        self.assertEqual(CoreTrial.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.tier(), "core")

    def test_a_failure_never_breaks_the_signup_nor_leaves_its_transaction_aborted(self):
        # A database error inside the savepoint (the OAuth callback calls this inside its own
        # transaction): swallowed, and the caller can still query afterwards.
        def real_failing_sql(*_args, **_kwargs):
            # Real SQL that Postgres refuses: it marks the open transaction as aborted, which a
            # Python-raised error would not.
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1/0")

        with (
            offer(True),
            mock.patch.object(first_month_trial, "_start", side_effect=real_failing_sql),
        ):
            self.assertIsNone(first_month_trial.start_for_new_account(self.user))
        self.assertEqual(CustomUser.objects.filter(pk=self.user.pk).count(), 1)
        self.assertFalse(CoreTrial.objects.exists())
        # And it still works once the fault is gone: nothing was half-written.
        self.assertIsNotNone(self.start())

    def test_verifying_an_email_signup_starts_the_trial_when_the_offer_is_on(self):
        newcomer = CustomUser.objects.create_user(
            email="signup@example.com", username="s", password="x"
        )
        raw = "t" * 32
        CustomUser.objects.filter(pk=newcomer.pk).update(
            email_verification_token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            email_verification_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        # Not verified yet: the unverified account has no trial.
        self.assertEqual(
            first_month_trial.why_not(newcomer), first_month_trial.NOT_ELIGIBLE_ACCOUNT
        )
        with offer(True):
            res = self.client.get(f"/api/v2/user/verify-email/?token={raw}")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(CoreTrial.objects.filter(user=newcomer).exists())
        self.assertEqual(self.tier(newcomer), "core")

    def test_verifying_an_email_signup_starts_nothing_while_the_offer_is_off(self):
        newcomer = CustomUser.objects.create_user(
            email="signup2@example.com", username="s2", password="x"
        )
        raw = "u" * 32
        CustomUser.objects.filter(pk=newcomer.pk).update(
            email_verification_token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            email_verification_token_expires_at=timezone.now() + timedelta(hours=1),
        )
        with offer(False):
            res = self.client.get(f"/api/v2/user/verify-email/?token={raw}")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(CoreTrial.objects.exists())

    def test_switching_the_offer_off_ends_it_for_new_people_and_not_for_a_running_trial(self):
        self.start()
        with offer(False):
            self.assertEqual(self.tier(), "core")  # the running trial keeps its days
            newcomer = verified(
                CustomUser.objects.create_user(email="new@example.com", username="n", password="x")
            )
            self.assertIsNone(first_month_trial.start_for_new_account(newcomer))
        self.assertEqual(self.tier(newcomer), "free")


class OnePerPersonTests(TrialBase):
    """One trial per person WHILE THE ACCOUNT EXISTS (the one-to-one `user` row). Nothing about the
    person is kept after the account is deleted (MIP-1779, counsel: no PII that is not necessary),
    so a person who deletes the account and signs up again may have a new first month."""

    def delete_account_of(self, user):
        # A solo account can be deleted: leave the shared team first.
        TeamMembers.objects.filter(attendee=user).update(is_deleted=True)
        account_deletion.delete_account(user)
        user.refresh_from_db()

    def test_deleting_the_account_leaves_no_trial_row_and_no_hash_anywhere(self):
        self.start(self.user2)
        address_hash = CustomUser.objects.get(pk=self.user2.pk).email_hash
        self.assertTrue(address_hash)
        self.assertTrue(CoreTrial.objects.filter(user=self.user2).exists())
        self.delete_account_of(self.user2)
        self.assertFalse(CoreTrial.objects.filter(user_id=self.user2.pk).exists())
        # The account row itself holds the hash of the made-up address, never the real one.
        self.assertNotEqual(CustomUser.objects.get(pk=self.user2.pk).email_hash, address_hash)
        # The trial table has no column that could hold an address hash, and the real hash is
        # in no row of it (read as text, whatever the columns are called).
        self.assertNotIn("email_hash", [f.name for f in CoreTrial._meta.get_fields()])
        with connection.cursor() as cursor:
            cursor.execute("SELECT t::text FROM origin_coretrial t")
            self.assertFalse([row for row in cursor.fetchall() if address_hash in row[0]])

    def test_signing_up_again_after_a_deletion_is_allowed_and_gets_a_new_first_month(self):
        self.start(self.user2)
        address = self.user2.email
        self.delete_account_of(self.user2)
        self.assertNotEqual(self.user2.email, address)  # anonymised
        again = verified(
            CustomUser.objects.create_user(email=address, username="back", password="x")
        )
        self.assertIsNone(first_month_trial.why_not(again))
        self.assertIsNotNone(self.start(again))
        self.assertEqual(self.tier(again), "core")
        self.assertEqual(CoreTrial.objects.filter(user=again).count(), 1)

    def test_an_existing_account_cannot_get_a_second_trial_even_after_the_first_ended(self):
        self.assertIsNotNone(self.start())
        self.end_now()
        self.assertEqual(first_month_trial.why_not(self.user), first_month_trial.ALREADY_HAD_ONE)
        self.assertIsNone(self.start())
        with offer(True):
            first_month_trial.grant_to_existing()
        self.assertEqual(CoreTrial.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.tier(), "free")

    def test_deleting_one_account_leaves_another_persons_running_trial_alone(self):
        self.start(self.user)
        before = CoreTrial.objects.get(user=self.user)
        self.start(self.user2)
        self.delete_account_of(self.user2)
        after = CoreTrial.objects.get(user=self.user)
        self.assertEqual((after.pk, after.ends_at), (before.pk, before.ends_at))
        self.assertEqual(self.tier(), "core")

    def test_the_migration_drops_the_rows_a_deleted_account_left_behind(self):
        """0255: a row kept by the old release for an already-deleted account goes, a live one stays
        (the data step only; the column drop is the schema change the test database already has)."""
        import importlib  # noqa: PLC0415

        from django.apps import apps  # noqa: PLC0415

        self.start(self.user)
        self.start(self.user2)
        CustomUser.objects.filter(pk=self.user2.pk).update(is_deleted=True, is_active=False)
        migration = importlib.import_module("origin.migrations.0255_core_trial_drop_email_hash")
        migration.drop_trials_of_deleted_accounts(apps, None)
        self.assertEqual(list(CoreTrial.objects.values_list("user_id", flat=True)), [self.user.pk])

    def test_the_account_system_matches_the_address_not_the_trial(self):
        self.start()
        # The account system lowercases (blind index), so an address that differs only in case is
        # the same account and cannot be registered twice: that unique `email_hash` is what keeps
        # "one per person" true while the account lives, with no copy on the trial row.
        self.assertTrue(CustomUser.objects.with_email("TEST@Example.com").exists())
        with self.assertRaises(Exception):  # noqa: B017, PT011 - IntegrityError, any backend
            with transaction.atomic():
                CustomUser.objects.create_user(
                    email="TEST@Example.com", username="twin", password="x"
                )
        self.assertEqual(CoreTrial.objects.count(), 1)


class ExistingAccountsTests(TrialBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("first_month_trial", *args, stdout=out)
        return out.getvalue()

    def test_the_command_refuses_while_the_offer_is_off(self):
        with offer(False):
            with self.assertRaises(CommandError):
                self.run_command("grant-existing")
            with self.assertRaises(RuntimeError):
                first_month_trial.grant_to_existing()
        self.assertFalse(CoreTrial.objects.exists())

    def test_a_dry_run_writes_nothing_and_apply_needs_yes(self):
        with offer(True):
            text = self.run_command("grant-existing")
            self.assertIn("would start: 2", text)
            with self.assertRaises(CommandError):
                self.run_command("grant-existing", "--apply")
        self.assertFalse(CoreTrial.objects.exists())

    def test_existing_accounts_get_it_once_from_now_never_back_dated(self):
        CustomUser.objects.filter(pk__in=[self.user.pk, self.user2.pk]).update(
            ts_created_at=timezone.now() - timedelta(days=400)
        )
        before = timezone.now()
        with offer(True):
            self.assertIn("started: 2", self.run_command("grant-existing", "--apply", "--yes"))
            for trial in CoreTrial.objects.all():
                self.assertGreaterEqual(trial.started_at, before)
                self.assertEqual(trial.ends_at - trial.started_at, timedelta(days=30))
            # Run again: nobody gets a second one.
            self.assertIn("started: 0", self.run_command("grant-existing", "--apply", "--yes"))
        self.assertEqual(CoreTrial.objects.count(), 2)
        self.assertEqual(self.tier(), "core")

    def test_payers_comps_paying_teams_and_demo_accounts_are_skipped_not_burned(self):
        payer = verified(
            CustomUser.objects.create_user(email="pay@example.com", username="p", password="x")
        )
        comp = verified(
            CustomUser.objects.create_user(email="comp@example.com", username="c", password="x")
        )
        demo = verified(
            CustomUser.objects.create_user(email="demo@example.com", username="d", password="x")
        )
        CustomUser.objects.filter(pk=payer.pk).update(tier="pro")
        CustomUser.objects.filter(pk=comp.pk).update(tier="max", tier_set_by=TIER_SET_BY_OPERATOR)
        CustomUser.objects.filter(pk=demo.pk).update(is_demo=True)
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="pro")  # lifts user and user2
        with offer(True):
            summary = first_month_trial.grant_to_existing()
        self.assertEqual(summary.started, 0)
        self.assertFalse(CoreTrial.objects.exists())
        self.assertEqual(summary.skipped[first_month_trial.ALREADY_ON_A_PLAN], 4)

    def test_status_reports_the_offer_and_the_counts(self):
        self.start()
        self.end_now()
        self.start(self.user2)
        with offer(True):
            text = self.run_command("status")
        self.assertIn("offer: ON", text)
        self.assertIn("2 started, 1 running, 1 ended", text)


class PayersAreNeverTouchedTests(TrialBase):
    def test_a_payer_keeps_their_plan_and_is_not_given_a_trial(self):
        CustomUser.objects.filter(pk=self.user.pk).update(tier="pro")
        self.assertEqual(first_month_trial.why_not(self.user), first_month_trial.ALREADY_ON_A_PLAN)
        self.assertIsNone(self.start())
        self.assertEqual(self.tier(), "pro")
        self.assertFalse(CoreTrial.objects.exists())

    def test_a_trial_row_never_lowers_a_higher_plan(self):
        self.start()
        CustomUser.objects.filter(pk=self.user.pk).update(tier="max")
        self.assertEqual(self.tier(), "max")
        self.assertIsNone(quota.resolve_effective_tier(str(self.user.id))["trial_ends_at"])
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="enterprise")
        self.assertEqual(self.tier(), "enterprise")

    def test_a_paying_teams_plan_wins_over_the_trial(self):
        self.start(self.user2)
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="pro")
        quota.invalidate_effective_tier([str(self.user2.id)])
        resolved = quota.resolve_effective_tier(str(self.user2.id))
        self.assertEqual((resolved["tier"], resolved["source"]), ("pro", "team"))
        self.assertIsNone(resolved["trial_ends_at"])


@override_settings(STRIPE=STRIPE_TEST_SETTINGS)
class StripeCannotEndItEarlyTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        verified(self.user)
        with offer(True):
            self.trial = first_month_trial.start_for_new_account(self.user)
        self.assertIsNotNone(self.trial)
        CustomUser.objects.filter(pk=self.user.pk).update(stripe_customer_id="cus_abc")
        self.user.refresh_from_db()

    def tier(self):
        quota.invalidate_effective_tier([str(self.user.id)])
        return quota.get_effective_tier(str(self.user.id))

    def test_webhook_events_that_say_free_do_not_end_the_trial(self):
        for etype, status_ in [
            ("customer.subscription.deleted", "canceled"),
            ("customer.subscription.updated", "unpaid"),
            ("customer.subscription.updated", "canceled"),
        ]:
            with self.subTest(etype=etype, status=status_):
                stripe_billing.handle_event(self.subscription_event(etype=etype, status_=status_))
                self.assertEqual(self.tier(), "core")
        self.assertTrue(CoreTrial.objects.filter(pk=self.trial.pk).exists())

    def test_a_reconcile_that_finds_no_subscription_does_not_end_it(self):
        payload = {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"}
        with mock.patch(
            "stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(payload, "sk_test_x"),
        ):
            stripe_billing.reconcile_from_stripe(self.user)
        self.assertEqual(self.tier(), "core")

    def test_subscribing_during_the_trial_works_and_the_plan_outlives_it(self):
        stripe_billing.handle_event(self.checkout_completed_event(plan="pro"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.tier, "pro")
        self.assertEqual(self.tier(), "pro")
        self.assertIsNone(quota.resolve_effective_tier(str(self.user.id))["trial_ends_at"])
        CoreTrial.objects.filter(pk=self.trial.pk).update(
            ends_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(self.tier(), "pro")

    def test_the_billing_page_still_offers_checkout_to_a_trial_account(self):
        # Checkout is offered to `personal_tier == "free"`, which a trial never changes.
        res = self.client.get("/api/v2/billing/config/")
        self.assertEqual(res.data["personal_tier"], "free")


class DayThirtyTests(TrialBase):
    def test_the_plan_reads_as_before_and_the_data_is_all_still_there(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Keep",
            code="KEP",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=self.user,
            title="Mine",
            status="Open",
            project_task_number=1,
        )
        self.start()
        self.assertEqual(self.tier(), "core")
        self.end_now()
        self.assertEqual(self.tier(), "free")
        self.assertEqual(TaskMaster.objects.filter(title="Mine").count(), 1)
        self.assertEqual(CoreTrial.objects.filter(user=self.user).count(), 1)
        # Nothing was written at day 30: the user row is exactly what it was.
        self.user.refresh_from_db()
        self.assertEqual((self.user.tier, self.user.tier_set_by), ("free", "stripe"))

    def test_an_ended_trial_is_not_given_again_and_the_offer_does_not_reopen_it(self):
        self.start()
        self.end_now()
        with offer(True):
            self.assertEqual(
                first_month_trial.why_not(self.user), first_month_trial.ALREADY_HAD_ONE
            )
            self.assertIsNone(first_month_trial.start_for_new_account(self.user))
            summary = first_month_trial.grant_to_existing()
        self.assertEqual(summary.started, 1)  # user2 only
        self.assertEqual(self.tier(), "free")

    def test_the_cache_never_outlives_the_trial(self):
        self.start()
        CoreTrial.objects.filter(user=self.user).update(
            ends_at=timezone.now() + timedelta(seconds=5)
        )
        quota.invalidate_effective_tier([str(self.user.id)])
        with mock.patch.object(quota.cache, "set") as cache_set:
            quota.resolve_effective_tier(str(self.user.id))
        self.assertLessEqual(cache_set.call_args.args[2], 5)

    def test_resolving_a_plan_costs_two_queries_with_or_without_a_trial(self):
        for with_trial in (False, True):
            if with_trial:
                self.start()
            quota.invalidate_effective_tier([str(self.user.id)])
            with self.assertNumQueries(2):  # the user + trial join, then the team plans
                quota.resolve_effective_tier(str(self.user.id))

    def test_scheduled_reports_follow_the_plan_and_the_default_seed_scan_sees_a_trial(self):
        table = {**settings.SEARCH_ENGINE, "TIER_QUOTAS": settings.PRODUCTION_TIER_QUOTAS}
        RefreshToken.for_user(self.user2)  # a recent sign-in, as `pending_members` requires
        with override_settings(SEARCH_ENGINE=table):
            pending = lambda: {u.pk for u in schedule_defaults.pending_members()}  # noqa: E731
            self.assertNotIn(self.user2.pk, pending())  # Free, no trial: left out of the scan
            self.start(self.user2)
            self.assertIn(self.user2.pk, pending())  # the trial is Core: in the scan
            self.end_now(self.user2)
            self.assertNotIn(self.user2.pk, pending())


@oauth_on()
class ConnectedAppsAreKeptNotErasedTests(TrialBase):
    def setUp(self):
        super().setUp()
        self.app = Application.objects.create(
            client_id="claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
            name="Claude",
        )
        self.conn = McpConnection.objects.create(
            user=self.user,
            application=self.app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="G",
            code="GAT",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        raw = generate_key()
        self.key = ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.raw_key = raw

    def listed(self):
        self.authenticate()
        rows = self.client.get(CONNECTIONS).json()["connections"]
        self.client.credentials()
        return rows

    def mcp(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {self.raw_key}")
        quota.invalidate_effective_tier([str(self.user.id)])
        res = self.client.post(
            MCP, {"jsonrpc": "2.0", "method": "tools/list", "id": 1}, format="json"
        )
        self.client.credentials()
        return res.status_code

    def test_free_then_trial_then_day_thirty_then_a_paid_plan(self):
        # Free: the connection exists but is paused, and the plan gate refuses its calls.
        self.assertEqual([r["paused"] for r in self.listed()], [True])
        self.assertEqual(self.mcp(), 403)
        # The trial: live, and the same key reaches the tools.
        self.assertIsNotNone(self.start())
        self.assertEqual(self.tier(), "core")
        self.assertEqual([r["paused"] for r in self.listed()], [False])
        self.assertEqual(self.mcp(), 200)
        # Day 30: refused again and shown paused, but NOT deleted, still endable.
        self.end_now()
        self.assertEqual([r["paused"] for r in self.listed()], [True])
        self.assertEqual(self.mcp(), 403)
        self.assertTrue(McpConnection.objects.get(pk=self.conn.pk).is_active)
        self.assertTrue(ApiKey.objects.filter(pk=self.key.pk, revoked_at__isnull=True).exists())
        # A paid plan: the very same connection and key work again, nothing was re-created.
        CustomUser.objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertEqual([r["paused"] for r in self.listed()], [False])
        self.assertEqual(self.mcp(), 200)
        self.assertEqual(McpConnection.objects.filter(pk=self.conn.pk).count(), 1)

    def test_a_paused_connection_can_still_be_ended(self):
        self.authenticate()
        res = self.client.delete(f"{CONNECTIONS}{self.conn.id}/")
        self.assertEqual(res.status_code, 204)

    def test_rest_keys_are_not_a_trial_matter(self):
        # The public REST API is on every plan (the MCP refusal says so); a key's listing is
        # unchanged by the trial and by its end.
        self.authenticate()
        before = self.client.get("/api/v2/api-keys/").json()
        self.start()
        self.end_now()
        self.assertEqual(self.client.get("/api/v2/api-keys/").json(), before)

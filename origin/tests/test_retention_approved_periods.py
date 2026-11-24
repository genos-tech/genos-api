"""The retention periods Ken approved on 2026-11-24 (D5 of MIP-2200, task MIP-2252).

Each class is about one new way a retention job can go wrong: a purge that clears too much
(a status change losing its value), too little (a row found again on every run), or breaks
what reads the row afterwards (a restore button on a body that is gone).
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from origin.models.common.security_event_models import (
    AppendOnlyError,
    SecurityAction,
    SecurityEvent,
)
from origin.models.common.user_models import CustomUser
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.services import retention, stripe_retention
from origin.services.task_body_restore import RestoreRefused, restore_task_body
from origin.tests.test_base import BaseAPITestCase

OLD = timezone.now() - timedelta(days=400)


def _env(**periods):
    return mock.patch.dict(
        "os.environ", {f"RETENTION_{k.upper()}_DAYS": str(v) for k, v in periods.items()}
    )


def _days_ago(days):
    return timezone.now() - timedelta(days=days)


class ApprovedNumbersTests(BaseAPITestCase):
    def test_every_approved_period_compiles_against_the_database(self):
        """A bad join only fails the first time a period is set."""
        for p in retention.POLICIES:
            if p.approved_days is None:
                continue
            with self.subTest(policy=p.name):
                self.assertEqual(retention.preview_approved(p).matched, 0)

    def test_the_numbers_are_the_approved_ones(self):
        by = {p.name: p.approved_days for p in retention.POLICIES}
        self.assertEqual(by["task_activities"], 365)
        self.assertEqual(by["task_activity_bodies"], 30)
        self.assertEqual(by["note_versions"], 90)
        self.assertEqual(by["webhook_deliveries"], 30)
        self.assertEqual(by["agent_step_results"], 30)
        self.assertEqual(by["agent_runs"], 180)
        self.assertEqual(by["security_event_network"], 90)

    def test_the_list_names_the_new_variables(self):
        out = StringIO()
        call_command("retention_purge", "--list", stdout=out)
        for name in ("TASK_ACTIVITY_BODIES", "AGENT_SESSIONS", "SECURITY_EVENT_NETWORK"):
            self.assertIn(f"RETENTION_{name}_DAYS", out.getvalue())


class TaskHistoryBodyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.task = TaskMaster.objects.create(
            team=self.team, title="t", status="Open", reporter=self.user
        )
        TaskActivity.objects.filter(task=self.task).delete()

    def _row(self, *, days_old, action=TaskActivityActionType.DESCRIPTION, kept=True, **extra):
        row = TaskActivity.objects.create(
            task=self.task,
            team=self.team,
            action_type=action,
            field_name="content",
            old_value=[{"type": "paragraph", "text": "the old secret body"}] if kept else None,
            metadata={"previousBodyKept": True, "via": {"kind": "api_key"}} if kept else {},
            **extra,
        )
        TaskActivity.objects.filter(pk=row.pk).update(ts_created_at=_days_ago(days_old))
        return row

    def test_a_kept_body_is_cleared_after_30_days_and_the_rest_of_the_row_stays(self):
        row = self._row(days_old=31)

        with _env(task_activity_bodies=30):
            result = retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        row.refresh_from_db()
        self.assertEqual(result.changed, 1)
        self.assertIsNone(row.old_value)
        self.assertEqual(row.action_type, TaskActivityActionType.DESCRIPTION)
        self.assertEqual(row.metadata, {"via": {"kind": "api_key"}})

    def test_a_body_inside_30_days_stays_and_can_still_be_restored(self):
        row = self._row(days_old=10)

        with _env(task_activity_bodies=30):
            retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        row.refresh_from_db()
        self.assertIsNotNone(row.old_value)
        self.assertTrue(row.metadata["previousBodyKept"])

    def test_a_cleared_row_no_longer_offers_a_restore(self):
        row = self._row(days_old=40)

        with _env(task_activity_bodies=30):
            retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        with self.assertRaises(RestoreRefused) as caught:
            restore_task_body(task=self.task, activity_id=row.activity_id, actor_id=self.user.id)
        self.assertEqual(caught.exception.status, 400)

    def test_a_second_run_finds_nothing(self):
        self._row(days_old=40)

        with _env(task_activity_bodies=30):
            first = retention.run_policy(retention.BY_NAME["task_activity_bodies"])
            second = retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        self.assertEqual((first.changed, second.changed), (1, 0))

    def test_a_status_change_keeps_its_values_whatever_its_age(self):
        """Closed counts and throughput read `new_value` of a status row."""
        row = TaskActivity.objects.create(
            task=self.task,
            team=self.team,
            action_type=TaskActivityActionType.STATUS,
            field_name="status",
            old_value="Open",
            new_value="Closed",
        )
        TaskActivity.objects.filter(pk=row.pk).update(ts_created_at=OLD)

        with _env(task_activity_bodies=30):
            retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        row.refresh_from_db()
        self.assertEqual((row.old_value, row.new_value), ("Open", "Closed"))

    def test_a_description_row_with_no_kept_body_is_not_touched(self):
        row = self._row(days_old=400, kept=False)

        with _env(task_activity_bodies=30):
            result = retention.run_policy(retention.BY_NAME["task_activity_bodies"])

        self.assertEqual(result.changed, 0)
        self.assertTrue(TaskActivity.objects.filter(pk=row.pk).exists())

    def test_the_deleted_row_of_a_still_deleted_task_stays_so_its_purge_can_date_it(self):
        """`purge_deleted_tasks` dates a deletion by this row; without it the task reads as
        "no time" and is never purged."""
        TaskMaster.objects.filter(pk=self.task.pk).update(status="Deleted")
        deleted = self._row(
            days_old=500,
            kept=False,
            action=TaskActivityActionType.STATUS,
        )
        TaskActivity.objects.filter(pk=deleted.pk).update(
            field_name="status", old_value="Open", new_value="Deleted"
        )
        other = self._row(days_old=500, kept=False, action=TaskActivityActionType.TITLE)

        with _env(task_activities=365):
            retention.run_policy(retention.BY_NAME["task_activities"])

        self.assertTrue(TaskActivity.objects.filter(pk=deleted.pk).exists())
        self.assertFalse(TaskActivity.objects.filter(pk=other.pk).exists())

    def test_the_deleted_row_of_a_task_that_is_no_longer_deleted_goes_with_the_rest(self):
        row = self._row(days_old=500, kept=False, action=TaskActivityActionType.STATUS)
        TaskActivity.objects.filter(pk=row.pk).update(
            field_name="status", old_value="Open", new_value="Deleted"
        )

        with _env(task_activities=365):
            retention.run_policy(retention.BY_NAME["task_activities"])

        self.assertFalse(TaskActivity.objects.filter(pk=row.pk).exists())

    def test_the_whole_row_goes_at_365_days_and_the_task_stays(self):
        old = self._row(days_old=366, kept=False)
        young = self._row(days_old=364, kept=False)

        with _env(task_activities=365):
            retention.run_policy(retention.BY_NAME["task_activities"])

        self.assertFalse(TaskActivity.objects.filter(pk=old.pk).exists())
        self.assertTrue(TaskActivity.objects.filter(pk=young.pk).exists())
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())


class AgentSessionTests(BaseAPITestCase):
    def _session(self, days_idle):
        s = AgentSession.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            rolling_summary_text="what we talked about",
        )
        AgentSession.objects.filter(pk=s.pk).update(last_active_at=_days_ago(days_idle))
        return s

    def _run(self, session, days_old, status="done"):
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            query="q",
            status=status,
            session=session,
        )
        AgentRun.objects.filter(pk=run.pk).update(started_at=_days_ago(days_old))
        return run

    def test_a_session_with_no_run_left_goes_with_its_summary(self):
        s = self._session(200)

        with _env(agent_sessions=180):
            retention.run_policy(retention.BY_NAME["agent_sessions"])

        self.assertFalse(AgentSession.objects.filter(pk=s.pk).exists())

    def test_a_session_with_a_run_left_stays(self):
        s = self._session(200)
        self._run(s, days_old=5)

        with _env(agent_sessions=180):
            retention.run_policy(retention.BY_NAME["agent_sessions"])

        self.assertTrue(AgentSession.objects.filter(pk=s.pk).exists())

    def test_a_session_with_a_run_awaiting_approval_stays_however_old(self):
        s = self._session(400)
        self._run(s, days_old=400, status="awaiting_approval")

        with _env(agent_runs=180, agent_sessions=180):
            retention.run_all()

        self.assertTrue(AgentSession.objects.filter(pk=s.pk).exists())

    def test_a_run_that_starts_between_choosing_and_deleting_keeps_its_session(self):
        """Thread, note and task sessions can be resumed after any time. The purge looks again
        at write time, so the new run is not detached from its session."""
        s = self._session(200)
        chosen = mock.MagicMock()
        chosen.values_list.return_value = [s.pk]  # what the purge picked a moment ago
        run = self._run(s, days_old=0)

        done = retention._apply(retention.BY_NAME["agent_sessions"], chosen, 10)

        self.assertEqual(done, 1)  # counted as picked, but nothing was written
        self.assertTrue(AgentSession.objects.filter(pk=s.pk).exists())
        run.refresh_from_db()
        self.assertEqual(run.session_id, s.pk)

    def test_a_recent_empty_session_stays(self):
        s = self._session(1)

        with _env(agent_sessions=180):
            retention.run_policy(retention.BY_NAME["agent_sessions"])

        self.assertTrue(AgentSession.objects.filter(pk=s.pk).exists())

    def test_it_goes_with_its_last_run_in_one_pass(self):
        s = self._session(200)
        run = self._run(s, days_old=200)

        with _env(agent_runs=180, agent_sessions=180):
            retention.run_all()

        self.assertFalse(AgentRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(AgentSession.objects.filter(pk=s.pk).exists())

    def test_a_step_summary_is_cleared_when_the_step_output_is(self):
        run = self._run(None, days_old=40)
        step = AgentStep.objects.create(
            run=run, step_index=0, summary="Signed in as a@example.com", result_json={"a": 1}
        )
        # A step with a summary but no stored output is still cleared.
        text_only = AgentStep.objects.create(
            run=run, step_index=1, summary="Signed in as a@example.com", result_json=None
        )
        AgentStep.objects.filter(pk__in=[step.pk, text_only.pk]).update(created_at=_days_ago(40))

        with _env(agent_step_results=30):
            first = retention.run_policy(retention.BY_NAME["agent_step_results"])
            second = retention.run_policy(retention.BY_NAME["agent_step_results"])

        step.refresh_from_db()
        text_only.refresh_from_db()
        self.assertEqual((step.summary, step.result_json), ("", None))
        self.assertEqual(text_only.summary, "")
        self.assertEqual((first.changed, second.changed), (2, 0))
        self.assertTrue(AgentRun.objects.filter(pk=run.pk).exists())


class SecurityEventNetworkTests(BaseAPITestCase):
    NAME = "security_event_network"

    def _event(
        self, days_old, action=SecurityAction.SIGNIN_FAILED, ip="203.0.113.7", ua="Mozilla/5.0 test"
    ):
        event = SecurityEvent.objects.create(
            action=action,
            source="anonymous",
            actor_id=self.user.pk,
            actor_label="someone",
            ip=ip,
            user_agent=ua,
        )
        with connection.cursor() as cur:
            cur.execute(
                f"UPDATE {SecurityEvent._meta.db_table} SET ts_created_at = %s WHERE event_id = %s",
                [_days_ago(days_old), event.event_id],
            )
        return event

    def test_the_address_and_browser_go_after_90_days_and_the_event_stays(self):
        old = self._event(100)

        with _env(**{self.NAME: 90}):
            result = retention.run_policy(retention.BY_NAME[self.NAME])

        old.refresh_from_db()
        self.assertEqual(result.changed, 1)
        self.assertIsNone(old.ip)
        self.assertEqual(old.user_agent, "")
        self.assertEqual(old.action, SecurityAction.SIGNIN_FAILED)
        self.assertEqual(old.actor_id, self.user.pk)
        self.assertEqual(old.actor_label, "someone")

    def test_a_recent_event_keeps_its_address(self):
        young = self._event(10)

        with _env(**{self.NAME: 90}):
            retention.run_policy(retention.BY_NAME[self.NAME])

        young.refresh_from_db()
        self.assertEqual(young.ip, "203.0.113.7")
        self.assertEqual(young.user_agent, "Mozilla/5.0 test")

    def test_a_period_under_the_floor_is_read_as_90(self):
        near = self._event(30)

        with _env(**{self.NAME: 7}):
            retention.run_policy(retention.BY_NAME[self.NAME])

        near.refresh_from_db()
        self.assertEqual(near.ip, "203.0.113.7")

    def test_a_second_run_finds_nothing(self):
        self._event(100)
        self._event(120, ip=None, ua="")  # already blank: not counted

        with _env(**{self.NAME: 90}):
            first = retention.run_policy(retention.BY_NAME[self.NAME])
            second = retention.run_policy(retention.BY_NAME[self.NAME])

        self.assertEqual((first.changed, second.changed), (1, 0))

    def test_a_backlog_of_more_than_one_batch_finishes(self):
        """The blanking helper loops over the batch it is given until it gets fewer rows than
        its size. A full batch must not make it pick the same rows again for ever."""
        count = retention._BATCH + 1
        SecurityEvent.objects.bulk_create(
            [
                SecurityEvent(
                    action=SecurityAction.SIGNIN_FAILED,
                    source="anonymous",
                    ip="203.0.113.7",
                    user_agent="ua",
                    ts_created_at=_days_ago(100),
                )
                for _ in range(count)
            ]
        )

        with _env(**{self.NAME: 90}):
            result = retention.run_policy(retention.BY_NAME[self.NAME])

        self.assertEqual(result.changed, count)
        self.assertFalse(SecurityEvent.objects.filter(ip__isnull=False).exists())

    def test_the_log_is_still_append_only_afterwards(self):
        self._event(100)

        with _env(**{self.NAME: 90}):
            retention.run_policy(retention.BY_NAME[self.NAME])

        with self.assertRaises(AppendOnlyError):
            SecurityEvent.objects.update(ip=None)

    def test_the_command_is_a_dry_run_until_apply(self):
        old = self._event(100)

        with _env(**{self.NAME: 90}):
            call_command("retention_purge", "--only", self.NAME, stdout=StringIO())
            old.refresh_from_db()
            self.assertEqual(old.ip, "203.0.113.7")
            call_command("retention_purge", "--apply", "--only", self.NAME, stdout=StringIO())
        old.refresh_from_db()
        self.assertIsNone(old.ip)


class StripeRedactionTests(BaseAPITestCase):
    def _person(self, *, deleted, years_ago, customer="cus_abc", email=None):
        user = CustomUser.objects.create_user(
            email=email or f"p{CustomUser.objects.count()}@example.com",
            username=f"p{CustomUser.objects.count()}",
            password="x" * 24,
        )
        CustomUser.objects.filter(pk=user.pk).update(
            is_deleted=deleted,
            stripe_customer_id=customer,
            ts_updated_at=timezone.now() - timedelta(days=int(365.25 * years_ago)),
        )
        return user

    def _stripe(self, *, side_effect=None, subscriptions=(), invoice_years_ago=None):
        """A fake Stripe: no subscription and no invoice unless asked."""
        fake = mock.MagicMock()
        fake.Customer.modify.side_effect = side_effect
        fake.Subscription.list.return_value = mock.Mock(
            data=[mock.Mock(status=s) for s in subscriptions]
        )
        invoices = []
        if invoice_years_ago is not None:
            created = timezone.now() - timedelta(days=int(365.25 * invoice_years_ago))
            invoices = [mock.Mock(created=int(created.timestamp()))]
        fake.Invoice.list.return_value = mock.Mock(data=invoices)
        return mock.patch("origin.services.stripe_billing._stripe", return_value=fake), fake

    def test_a_customer_that_may_still_be_billed_is_left_alone(self):
        user = self._person(deleted=True, years_ago=8, customer="cus_live")
        patcher, fake = self._stripe(subscriptions=("canceled", "active"))

        with patcher:
            out = stripe_retention.redact(apply=True)

        self.assertEqual((out.skipped, out.redacted), (1, 0))
        fake.Customer.modify.assert_not_called()
        user.refresh_from_db()
        self.assertEqual(user.stripe_customer_id, "cus_live")

    def test_a_customer_with_only_cancelled_subscriptions_is_redacted(self):
        self._person(deleted=True, years_ago=8)
        patcher, fake = self._stripe(subscriptions=("canceled",), invoice_years_ago=7.5)

        with patcher:
            out = stripe_retention.redact(apply=True)

        self.assertEqual(out.redacted, 1)

    def test_an_invoice_inside_its_own_seven_years_keeps_the_customer(self):
        self._person(deleted=True, years_ago=8)
        patcher, fake = self._stripe(invoice_years_ago=3)

        with patcher:
            out = stripe_retention.redact(apply=True)

        self.assertEqual((out.skipped, out.redacted), (1, 0))
        fake.Customer.modify.assert_not_called()

    def test_the_period_is_seven_years_with_the_leap_days(self):
        self.assertEqual(stripe_retention.BOOKKEEPING_DAYS, 2557)

    def test_only_a_deleted_account_past_seven_years_with_a_customer_is_a_candidate(self):
        due = self._person(deleted=True, years_ago=7.1)
        self._person(deleted=True, years_ago=6.9)  # still inside the bookkeeping period
        self._person(deleted=False, years_ago=9)  # a live account is never touched
        self._person(deleted=True, years_ago=9, customer=None)  # nothing at Stripe
        self._person(deleted=True, years_ago=9, customer="")

        self.assertEqual(list(stripe_retention.candidates()), [due])

    def test_a_dry_run_counts_and_calls_nothing(self):
        self._person(deleted=True, years_ago=8)
        patcher, fake = self._stripe()

        with patcher:
            out = stripe_retention.redact(apply=False)

        self.assertEqual((out.matched, out.dry_run), (1, True))
        fake.Customer.modify.assert_not_called()

    def test_apply_clears_name_and_email_at_stripe_and_the_local_pointer(self):
        user = self._person(deleted=True, years_ago=8, customer="cus_old")
        before = CustomUser.objects.get(pk=user.pk).ts_updated_at
        patcher, fake = self._stripe()

        with patcher:
            out = stripe_retention.redact(apply=True)

        fake.Customer.modify.assert_called_once_with("cus_old", name="", email="", phone="")
        self.assertEqual(out.redacted, 1)
        user.refresh_from_db()
        self.assertIsNone(user.stripe_customer_id)
        self.assertEqual(user.ts_updated_at, before)

    def test_a_second_run_finds_nothing(self):
        self._person(deleted=True, years_ago=8)
        patcher, fake = self._stripe()

        with patcher:
            stripe_retention.redact(apply=True)
            second = stripe_retention.redact(apply=True)

        self.assertEqual(second.matched, 0)
        self.assertEqual(fake.Customer.modify.call_count, 1)

    def test_a_customer_stripe_no_longer_has_is_done(self):
        user = self._person(deleted=True, years_ago=8)

        class Missing(Exception):
            code = "resource_missing"

        patcher, _ = self._stripe(side_effect=Missing("No such customer"))
        with patcher:
            out = stripe_retention.redact(apply=True)

        self.assertEqual((out.gone, out.failed), (1, 0))
        user.refresh_from_db()
        self.assertIsNone(user.stripe_customer_id)

    def test_any_other_failure_keeps_the_pointer_and_fails_the_run_loudly(self):
        user = self._person(deleted=True, years_ago=8, customer="cus_keep")
        patcher, _ = self._stripe(side_effect=RuntimeError("503 for a@example.com"))

        with patcher, self.assertLogs("origin.retention", level="ERROR") as logs:
            out = stripe_retention.redact(apply=True)

        self.assertEqual((out.failed, out.redacted), (1, 0))
        user.refresh_from_db()
        self.assertEqual(user.stripe_customer_id, "cus_keep")
        self.assertNotIn("example.com", "\n".join(logs.output))
        self.assertNotIn("cus_keep", "\n".join(logs.output))

    def test_the_limit_bounds_one_run(self):
        for _ in range(3):
            self._person(deleted=True, years_ago=8)
        patcher, fake = self._stripe()

        with patcher:
            out = stripe_retention.redact(apply=True, limit=2)

        self.assertEqual(out.redacted, 2)
        self.assertEqual(stripe_retention.candidates().count(), 1)

    @override_settings(STRIPE={"SECRET_KEY": ""})
    def test_the_command_says_so_when_stripe_is_not_configured(self):
        self._person(deleted=True, years_ago=8)

        with self.assertRaises(CommandError):
            call_command("redact_deleted_stripe_customers", "--apply", stdout=StringIO())

    def test_the_command_prints_counts_only(self):
        self._person(deleted=True, years_ago=8, customer="cus_secretid", email="who@example.com")
        out = StringIO()

        call_command("redact_deleted_stripe_customers", stdout=out)

        self.assertIn("would redact 1 customers", out.getvalue())
        self.assertNotIn("cus_secretid", out.getvalue())
        self.assertNotIn("example.com", out.getvalue())

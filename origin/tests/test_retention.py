"""Per-table retention: off by default, and only ever the rows it should touch (MIP-728).

A retention job is the one piece of code whose bug is "deleted everything", so
the tests are about the ways that could happen: a period that is not set being
read as zero, an unfinished row going because it is old, a blanked message
losing its row, a typo in a setting deleting instead of keeping.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.webhook_models import WebhookDelivery, WebhookEndpoint
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.models import AgentRun, AgentStep
from origin.services import retention
from origin.tests.test_base import BaseAPITestCase

OLD = timezone.now() - timedelta(days=400)


def _env(**periods):
    """Set `RETENTION_<NAME>_DAYS` for the duration of a block."""
    return mock.patch.dict(
        "os.environ", {f"RETENTION_{k.upper()}_DAYS": str(v) for k, v in periods.items()}
    )


class ParseDaysTests(SimpleTestCase):
    def test_unset_and_every_word_for_forever_is_none(self):
        for raw in (None, "", "  ", "0", "forever", "OFF", "never", "none", "infinite", "inf"):
            with self.subTest(raw=raw):
                self.assertIsNone(retention.parse_days(raw))

    def test_a_positive_number_is_days(self):
        self.assertEqual(retention.parse_days("90"), 90)
        self.assertEqual(retention.parse_days(" 30 "), 30)

    def test_a_negative_number_is_forever_not_a_future_cutoff(self):
        self.assertIsNone(retention.parse_days("-5"))

    def test_a_typo_keeps_data_instead_of_deleting_it(self):
        for raw in ("3o", "ninety", "1.5", "90d", "1e2"):
            with self.subTest(raw=raw):
                self.assertIsNone(retention.parse_days(raw))


class RegistryTests(SimpleTestCase):
    def test_every_policy_is_off_when_nothing_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            for p in retention.POLICIES:
                os.environ.pop(p.env_var, None)
            for p in retention.POLICIES:
                with self.subTest(policy=p.name):
                    self.assertIsNone(p.days())
                    self.assertIsNone(p.cutoff())
                    self.assertIsNone(p.candidates())

    def test_names_and_variables_are_unique(self):
        names = [p.name for p in retention.POLICIES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len({p.env_var for p in retention.POLICIES}), len(names))

    def test_a_blanking_policy_names_what_to_blank_and_a_deleting_one_does_not(self):
        for p in retention.POLICIES:
            with self.subTest(policy=p.name):
                if p.action is retention.Action.BLANK:
                    self.assertTrue(p.blank_to)
                else:
                    self.assertIsNone(p.blank_to)

    def test_every_age_field_and_blank_column_is_a_real_column(self):
        """A misspelt column only fails the first time a period is set, which is
        the worst moment to find out."""
        for p in retention.POLICIES:
            with self.subTest(policy=p.name):
                model = retention.model_of(p)
                names = {f.name for f in model._meta.get_fields()}
                self.assertIn(p.age_field, names)
                for column in p.blank_to or {}:
                    self.assertIn(column, names)


class PolicyQueriesAgainstTheDatabaseTests(BaseAPITestCase):
    def test_every_policy_query_compiles_against_the_database(self):
        """A bad join or column only fails when a period is first set."""
        for p in retention.POLICIES:
            with self.subTest(policy=p.name), _env(**{p.name: 1}):
                self.assertGreaterEqual(p.candidates().count(), 0)


class AgentRunTests(BaseAPITestCase):
    def _run(self, status, started=OLD):
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q", status=status
        )
        AgentRun.objects.filter(pk=run.pk).update(started_at=started)
        return run

    def test_nothing_goes_when_no_period_is_set(self):
        self._run("done")

        results = retention.run_all(only="agent_runs")

        self.assertTrue(results[0].skipped)
        self.assertEqual(AgentRun.objects.count(), 1)

    def test_a_finished_old_run_goes_and_takes_its_steps(self):
        run = self._run("done")
        AgentStep.objects.create(run=run, step_index=0, result_json={"x": 1})

        with _env(agent_runs=90):
            result = retention.run_policy(retention.BY_NAME["agent_runs"])

        self.assertEqual(result.changed, 1)
        self.assertEqual(AgentRun.objects.count(), 0)
        self.assertEqual(AgentStep.objects.count(), 0)

    def test_a_recent_run_stays(self):
        self._run("done", started=timezone.now() - timedelta(days=10))

        with _env(agent_runs=90):
            retention.run_policy(retention.BY_NAME["agent_runs"])

        self.assertEqual(AgentRun.objects.count(), 1)

    def test_a_run_still_in_flight_stays_however_old(self):
        for status in ("running", "awaiting_approval"):
            self._run(status)

        with _env(agent_runs=90):
            retention.run_policy(retention.BY_NAME["agent_runs"])

        self.assertEqual(AgentRun.objects.count(), 2)

    def test_every_finished_status_goes(self):
        for status in ("done", "error", "step_cap", "cancelled", "rejected"):
            self._run(status)

        with _env(agent_runs=90):
            retention.run_policy(retention.BY_NAME["agent_runs"])

        self.assertEqual(AgentRun.objects.count(), 0)

    def test_step_results_are_blanked_and_the_step_and_run_stay(self):
        run = self._run("done")
        step = AgentStep.objects.create(
            run=run, step_index=0, summary="kept", result_json={"secret": "tool output"}
        )
        AgentStep.objects.filter(pk=step.pk).update(created_at=OLD)

        with _env(agent_step_results=90):
            retention.run_policy(retention.BY_NAME["agent_step_results"])

        step.refresh_from_db()
        self.assertIsNone(step.result_json)
        self.assertEqual(step.summary, "kept")
        self.assertTrue(AgentRun.objects.filter(pk=run.pk).exists())

    def test_step_results_of_an_unfinished_run_are_kept(self):
        run = self._run("running")
        step = AgentStep.objects.create(run=run, step_index=0, result_json={"x": 1})
        AgentStep.objects.filter(pk=step.pk).update(created_at=OLD)

        with _env(agent_step_results=90):
            retention.run_policy(retention.BY_NAME["agent_step_results"])

        step.refresh_from_db()
        self.assertEqual(step.result_json, {"x": 1})

    def test_an_already_blank_step_is_not_counted_again(self):
        run = self._run("done")
        step = AgentStep.objects.create(run=run, step_index=0, result_json=None)
        AgentStep.objects.filter(pk=step.pk).update(created_at=OLD)

        with _env(agent_step_results=90):
            result = retention.run_policy(retention.BY_NAME["agent_step_results"])

        self.assertEqual(result.changed, 0)


class WebhookAndEmailTests(BaseAPITestCase):
    def _delivery(self, status):
        endpoint = WebhookEndpoint.objects.filter(team=self.team).first() or (
            WebhookEndpoint.objects.create(
                team=self.team, url="https://example.com/h", created_by=self.user
            )
        )
        d = WebhookDelivery.objects.create(endpoint=endpoint, event="e", payload={"k": "v"})
        WebhookDelivery.objects.filter(pk=d.pk).update(status=status, ts_created_at=OLD)
        return d

    def _email(self, status):
        e = EmailNotificationEvent.objects.create(user=self.user, category="c", title="t", body="b")
        EmailNotificationEvent.objects.filter(pk=e.pk).update(status=status, ts_created_at=OLD)
        return e

    def test_only_settled_webhook_deliveries_go(self):
        W = WebhookDelivery
        settled = [self._delivery(W.STATUS_SENT), self._delivery(W.STATUS_FAILED)]
        owed = [self._delivery(W.STATUS_PENDING), self._delivery(W.STATUS_SENDING)]

        with _env(webhook_deliveries=30):
            retention.run_policy(retention.BY_NAME["webhook_deliveries"])

        for d in settled:
            self.assertFalse(W.objects.filter(pk=d.pk).exists())
        for d in owed:
            self.assertTrue(W.objects.filter(pk=d.pk).exists())

    def test_only_finished_email_rows_go(self):
        E = EmailNotificationEvent
        finished = [self._email(s) for s in (E.STATUS_SENT, E.STATUS_SKIPPED, E.STATUS_FAILED)]
        owed = [self._email(E.STATUS_PENDING), self._email(E.STATUS_SENDING)]

        with _env(email_notification_events=30):
            retention.run_policy(retention.BY_NAME["email_notification_events"])

        for e in finished:
            self.assertFalse(E.objects.filter(pk=e.pk).exists())
        for e in owed:
            self.assertTrue(E.objects.filter(pk=e.pk).exists())


class NoteAndTaskTests(BaseAPITestCase):
    def test_old_note_versions_go_and_recent_ones_stay(self):
        old = NoteVersionMaster.objects.create(
            team=self.team, note_type=1, note_id=1, version_no=1, editor=self.user
        )
        new = NoteVersionMaster.objects.create(
            team=self.team, note_type=1, note_id=1, version_no=2, editor=self.user
        )
        NoteVersionMaster.objects.filter(pk=old.pk).update(ts_created_at=OLD)

        with _env(note_versions=180):
            retention.run_policy(retention.BY_NAME["note_versions"])

        self.assertFalse(NoteVersionMaster.objects.filter(pk=old.pk).exists())
        self.assertTrue(NoteVersionMaster.objects.filter(pk=new.pk).exists())

    def test_old_task_history_goes_and_the_task_stays(self):
        task = TaskMaster.objects.create(
            team=self.team, title="t", status="Open", reporter=self.user
        )
        TaskActivity.objects.filter(task=task).delete()
        old = TaskActivity.objects.create(
            task=task, team=self.team, action_type=TaskActivityActionType.TITLE
        )
        TaskActivity.objects.filter(pk=old.pk).update(ts_created_at=OLD)

        with _env(task_activities=365):
            retention.run_policy(retention.BY_NAME["task_activities"])

        self.assertFalse(TaskActivity.objects.filter(pk=old.pk).exists())
        self.assertTrue(TaskMaster.objects.filter(pk=task.pk).exists())


class DeletedMessageTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="c", owner=self.user
        )

    def _message(self, deleted_at=None, text="secret words"):
        m = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=Message.objects.filter(channel=self.channel).count() + 1,
            body={"type": "doc", "text": text},
            body_text=text,
        )
        if deleted_at is not None:
            Message.objects.filter(pk=m.pk).update(deleted_at=deleted_at)
        return m

    def test_a_deleted_messages_text_is_cleared_and_the_row_stays(self):
        m = self._message(deleted_at=OLD)

        with _env(deleted_message_bodies=30):
            retention.run_policy(retention.BY_NAME["deleted_message_bodies"])

        m.refresh_from_db()
        self.assertEqual(m.body_text, "")
        self.assertEqual(m.body, {})
        self.assertEqual(m.deleted_at.date(), OLD.date())
        self.assertEqual(m.seq, 1)

    def test_a_live_message_is_never_touched_however_old(self):
        m = self._message(deleted_at=None)
        Message.objects.filter(pk=m.pk).update(ts_sent_at=OLD)

        with _env(deleted_message_bodies=1):
            result = retention.run_policy(retention.BY_NAME["deleted_message_bodies"])

        m.refresh_from_db()
        self.assertEqual(m.body_text, "secret words")
        self.assertEqual(result.changed, 0)

    def test_a_recently_deleted_message_keeps_its_text_through_the_grace_period(self):
        m = self._message(deleted_at=timezone.now() - timedelta(days=3))

        with _env(deleted_message_bodies=30):
            retention.run_policy(retention.BY_NAME["deleted_message_bodies"])

        m.refresh_from_db()
        self.assertEqual(m.body_text, "secret words")

    def test_a_second_run_finds_nothing(self):
        self._message(deleted_at=OLD)

        with _env(deleted_message_bodies=30):
            first = retention.run_policy(retention.BY_NAME["deleted_message_bodies"])
            second = retention.run_policy(retention.BY_NAME["deleted_message_bodies"])

        self.assertEqual((first.changed, second.changed), (1, 0))


class BatchingAndDryRunTests(BaseAPITestCase):
    def _runs(self, n):
        AgentRun.objects.bulk_create(
            AgentRun(
                team_id=str(self.team.team_id), user_id=str(self.user.id), query="q", status="done"
            )
            for _ in range(n)
        )
        AgentRun.objects.update(started_at=OLD)

    def test_dry_run_counts_and_changes_nothing(self):
        self._runs(3)

        with _env(agent_runs=90):
            result = retention.run_policy(retention.BY_NAME["agent_runs"], dry_run=True)

        self.assertEqual((result.matched, result.changed, result.dry_run), (3, 0, True))
        self.assertEqual(AgentRun.objects.count(), 3)

    def test_one_pass_is_capped_and_the_next_finishes_the_backlog(self):
        self._runs(5)

        with _env(agent_runs=90):
            first = retention.run_policy(retention.BY_NAME["agent_runs"], limit=2)
            second = retention.run_policy(retention.BY_NAME["agent_runs"], limit=2)
            third = retention.run_policy(retention.BY_NAME["agent_runs"], limit=2)

        self.assertEqual([first.changed, second.changed, third.changed], [2, 2, 1])
        self.assertEqual(AgentRun.objects.count(), 0)

    def test_batches_smaller_than_the_cap_still_cover_every_row(self):
        self._runs(7)

        with _env(agent_runs=90), mock.patch.object(retention, "_BATCH", 3):
            result = retention.run_policy(retention.BY_NAME["agent_runs"])

        self.assertEqual(result.changed, 7)
        self.assertEqual(AgentRun.objects.count(), 0)

    def test_the_cap_holds_when_it_is_larger_than_one_batch(self):
        """The cap is enforced even when it spans several batches. (The loop's
        `while done < limit` is redundant with the `min(_BATCH, limit - done)`
        slice, which yields an empty batch once the cap is reached, so removing
        either alone is an equivalent change; this test fails if BOTH go.)"""
        self._runs(9)

        with _env(agent_runs=90), mock.patch.object(retention, "_BATCH", 2):
            result = retention.run_policy(retention.BY_NAME["agent_runs"], limit=5)

        self.assertEqual(result.changed, 5)
        self.assertEqual(AgentRun.objects.count(), 4)

    def test_the_oldest_rows_go_first(self):
        self._runs(3)
        runs = list(AgentRun.objects.all())
        for i, run in enumerate(runs):
            AgentRun.objects.filter(pk=run.pk).update(started_at=OLD + timedelta(days=i))

        with _env(agent_runs=90):
            retention.run_policy(retention.BY_NAME["agent_runs"], limit=1)

        remaining = set(AgentRun.objects.values_list("pk", flat=True))
        self.assertNotIn(runs[0].pk, remaining)
        self.assertEqual(len(remaining), 2)


class OnePolicyDoesNotTouchAnotherTests(BaseAPITestCase):
    def test_setting_one_period_leaves_the_others_alone(self):
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q", status="done"
        )
        AgentRun.objects.filter(pk=run.pk).update(started_at=OLD)
        note = NoteVersionMaster.objects.create(
            team=self.team, note_type=1, note_id=1, version_no=1, editor=self.user
        )
        NoteVersionMaster.objects.filter(pk=note.pk).update(ts_created_at=OLD)

        with _env(note_versions=30):
            retention.run_all()

        self.assertFalse(NoteVersionMaster.objects.filter(pk=note.pk).exists())
        self.assertTrue(AgentRun.objects.filter(pk=run.pk).exists())

    def test_the_security_audit_log_is_not_a_policy(self):
        """It has its own command and a floor; a period here must not reach it."""
        self.assertNotIn("security_events", retention.BY_NAME)
        self.assertFalse(any("security" in p.name for p in retention.POLICIES))


class CommandTests(BaseAPITestCase):
    def _call(self, *args):
        out = StringIO()
        call_command("retention_purge", *args, stdout=out)
        return out.getvalue()

    def test_with_nothing_set_every_policy_reports_keeping_forever(self):
        out = self._call()

        for p in retention.POLICIES:
            self.assertIn(f"{p.name}: keeping forever", out)

    def test_list_shows_each_policy_and_its_variable(self):
        out = self._call("--list")

        for p in retention.POLICIES:
            self.assertIn(p.env_var, out)
            self.assertIn("forever", out)

    def test_list_shows_a_period_once_it_is_set(self):
        with _env(agent_runs=90):
            out = self._call("--list")

        self.assertRegex(out, r"agent_runs\s+delete\s+90 days")

    def test_only_runs_one_policy(self):
        with _env(agent_runs=90, note_versions=90):
            out = self._call("--only", "agent_runs", "--dry-run")

        self.assertIn("agent_runs", out)
        self.assertNotIn("note_versions", out)

    def test_an_unknown_policy_is_an_error_not_a_silent_no_op(self):
        with self.assertRaisesMessage(CommandError, "Unknown policy"):
            self._call("--only", "nope")

    def test_dry_run_is_labelled_and_deletes_nothing(self):
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q", status="done"
        )
        AgentRun.objects.filter(pk=run.pk).update(started_at=OLD)

        with _env(agent_runs=90):
            out = self._call("--only", "agent_runs", "--dry-run")

        self.assertIn("[dry-run] agent_runs: would change 1 rows", out)
        self.assertTrue(AgentRun.objects.filter(pk=run.pk).exists())

    def test_a_real_run_changes_rows_and_says_so(self):
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q", status="done"
        )
        AgentRun.objects.filter(pk=run.pk).update(started_at=OLD)

        with _env(agent_runs=90):
            out = self._call("--only", "agent_runs")

        self.assertIn("agent_runs: changed 1 rows older than 90 days", out)
        self.assertFalse(AgentRun.objects.filter(pk=run.pk).exists())


@override_settings()
class CronRegistrationTests(SimpleTestCase):
    def test_the_cron_is_watched_so_a_dead_one_is_noticed(self):
        from origin.services import ops_alerts

        self.assertIn("retention-purge", ops_alerts.CRON_SCHEDULES)

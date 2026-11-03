"""The one rule for "this `running` run was abandoned" (`stale_runs`).

`reap_stale_agent_runs` closes by it and the conversation delete refuses by
its complement; what each does with it is pinned where each is tested
(`test_agent_run_cancellation`, `test_agent_conversation_delete`). Here: the
constant, the cutoff, and that the two Q objects partition the `running` rows
with nothing left over and nothing in common.
"""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from origin.search_engine import stale_runs
from origin.search_engine.models import AgentRun

NOW = timezone.now()


class ConstantAndCutoffTests(SimpleTestCase):
    def test_the_threshold_is_two_hours(self):
        # What the cron has always used. Raising it delays reaping; lowering it
        # risks closing, or deleting under, a run that is still streaming.
        self.assertEqual(stale_runs.STALE_RUN_HOURS, 2)

    def test_the_cutoff_is_the_threshold_before_now(self):
        self.assertEqual(stale_runs.stale_cutoff(now=NOW), NOW - timedelta(hours=2))

    def test_an_explicit_hours_overrides_the_threshold(self):
        self.assertEqual(stale_runs.stale_cutoff(6, now=NOW), NOW - timedelta(hours=6))

    def test_the_threshold_is_read_at_call_time(self):
        with mock.patch.object(stale_runs, "STALE_RUN_HOURS", 5):
            self.assertEqual(stale_runs.stale_cutoff(now=NOW), NOW - timedelta(hours=5))

    def test_hours_below_one_are_clamped_to_one(self):
        for hours in (0, -3):
            with self.subTest(hours=hours):
                self.assertEqual(stale_runs.stale_cutoff(hours, now=NOW), NOW - timedelta(hours=1))

    def test_the_cutoff_defaults_to_the_current_time(self):
        before = timezone.now() - timedelta(hours=2)
        cutoff = stale_runs.stale_cutoff()
        after = timezone.now() - timedelta(hours=2)
        self.assertTrue(before <= cutoff <= after)


class PartitionTests(TestCase):
    """`stale_running_q` and `in_flight_q` split the `running` rows in two."""

    def make(self, status, age):
        run = AgentRun.objects.create(team_id="t", user_id="u", query="q", status=status)
        AgentRun.objects.filter(pk=run.pk).update(started_at=NOW - age)
        return run.pk

    def test_stale_and_in_flight_partition_the_running_rows(self):
        cutoff = stale_runs.stale_cutoff(now=NOW)
        stale = {
            self.make("running", timedelta(hours=2, microseconds=1)),
            self.make("running", timedelta(hours=9)),
        }
        in_flight = {
            self.make("running", timedelta(0)),
            self.make("running", timedelta(hours=2) - timedelta(microseconds=1)),
            self.make("running", timedelta(hours=2)),  # exactly on the cutoff
        }
        # Never in either, however old.
        for status in ("done", "error", "step_cap", "cancelled", "awaiting_approval", "rejected"):
            self.make(status, timedelta(days=30))

        stale_ids = set(
            AgentRun.objects.filter(stale_runs.stale_running_q(cutoff)).values_list("pk", flat=True)
        )
        flight_ids = set(
            AgentRun.objects.filter(stale_runs.in_flight_q(cutoff)).values_list("pk", flat=True)
        )

        self.assertEqual(stale_ids, stale)
        self.assertEqual(flight_ids, in_flight)
        self.assertEqual(stale_ids & flight_ids, set())
        self.assertEqual(
            stale_ids | flight_ids,
            set(AgentRun.objects.filter(status="running").values_list("pk", flat=True)),
        )

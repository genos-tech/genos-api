"""MIP-2457: "Run now" of a slow report never runs inside the web request, and a run that was
killed does not keep the schedule "running" for more than about a minute.

Production, 2026-11-27: a skill dashboard ran inside the request, passed gunicorn's timeout, the
worker was killed, and the one-time schedule stayed claimed for 15 minutes.

No child process is started here (`schedule_runner._spawn_process` is replaced): a spawn that
does nothing is exactly a child that was killed before it could record anything.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from origin.models.common.schedule_models import MemberSchedule
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_runner, schedules
from origin.services.schedule_jobs import JOBS, Job, register_job
from origin.tests.test_schedules import NOW, SCHEDULED, ScheduleBase, utc
from origin.tests.transaction_support import CheapFlushTransactionTestCase


def seconds(n):
    return timedelta(seconds=n)


class Proc:
    """A child that is already over."""

    def wait(self, timeout=None):
        return 0


@override_settings(SCHEDULE_RUN_NOW_MODE="subprocess")
class BackgroundBase(ScheduleBase):
    def setUp(self):
        super().setUp()
        register_job(Job("slow", "Slow", "A long model loop.", self._ok, unwrapped=True))
        self.spawned = []

        def spawn(schedule_id, claim):
            self.spawned.append((schedule_id, claim))
            return Proc()

        patcher = mock.patch.object(schedule_runner, "_spawn_process", spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        running = mock.patch.object(schedule_runner, "_RUNNING", set())
        running.start()
        self.addCleanup(running.stop)
        # The reaper thread frees the child's place; here the test says when.
        reaper = mock.patch.object(schedule_runner.threading, "Thread")
        reaper.start()
        self.addCleanup(reaper.stop)

    def once(self, job="slow", **kw):
        # Due at 09:00 Tokyo on the 9th; the tick has not come for it yet.
        return self.make(
            job=job,
            frequency="once",
            run_date="2026-11-09",
            due_at=utc(2026, 11, 9, 5),
            now=utc(2026, 11, 7),
            **kw,
        )

    def run_child(self):
        """What the child process does with what it was started with."""
        schedule_id, claim = self.spawned[-1]
        call_command("schedule_run_now", "--schedule", schedule_id, "--claim", claim)


class SlowRunsLeaveTheRequestTests(BackgroundBase):
    def test_a_slow_job_does_not_run_in_the_request(self):
        sched = self.once()
        out = schedules.run_now(self.ctx, sched.pk, now=NOW)
        self.assertEqual(self.calls, [])
        self.assertEqual(
            out,
            {
                "status": "started",
                "error": "",
                "title": "",
                "items": 0,
                "started_at": "2026-11-09T09:04+09:00",
            },
        )
        self.assertEqual(self.spawned, [(str(sched.pk), NOW.isoformat())])
        sched.refresh_from_db()
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (NOW, NOW))
        self.assertIsNone(sched.last_run_at)
        self.assertEqual(schedules.running_since(sched, NOW), NOW)

    def test_the_child_runs_it_records_it_and_lets_go(self):
        sched = self.once()
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        self.run_child()
        self.assertEqual(len(self.calls), 1)
        sched.refresh_from_db()
        self.assertEqual((sched.last_status, sched.last_run_at), ("ok", NOW))
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (None, None))
        self.assertIsNone(sched.next_run_at)  # a one-time schedule is used up (MIP-2403)
        self.assertEqual(sched.last_result, {"title": "Your day", "items": 2})

    def test_a_child_whose_claim_is_gone_runs_nothing(self):
        sched = self.once()
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        schedules.set_enabled(self.ctx, sched.pk, False)  # the member paused it meanwhile
        self.run_child()
        self.assertEqual(self.calls, [])
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "")

    def test_an_edit_during_a_run_releases_the_claim_and_its_heartbeat(self):
        sched = self.once()
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        schedules.update(self.ctx, sched.pk, name="Renamed")
        sched.refresh_from_db()
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (None, None))

    def test_ai_and_dashboard_jobs_are_slow_and_plain_ones_are_not(self):
        for job, slow in (
            (Job("x", "x", "x", self._ok, unwrapped=True), True),
            (Job("x", "x", "x", self._ok, delivers_note=True), True),
            (Job("x", "x", "x", self._ok, uses_ai=True), True),
            (Job("x", "x", "x", self._ok), False),
        ):
            with mock.patch.dict(JOBS, {"x": job}):
                sched = MemberSchedule(job="x", params={})
                self.assertEqual(
                    schedules.runs_in_background(sched), job.ai_applies({}) or slow, job
                )

    def test_a_plain_job_still_runs_in_the_request(self):
        sched = self.once(job="ok")
        out = schedules.run_now(self.ctx, sched.pk, now=NOW)
        self.assertEqual(out["status"], "ok")
        self.assertEqual((len(self.calls), self.spawned), (1, []))
        sched.refresh_from_db()
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (None, None))

    @override_settings(SCHEDULE_RUN_NOW_MODE="sync")
    def test_the_rollback_mode_runs_everything_in_the_request(self):
        sched = self.once()
        self.assertEqual(schedules.run_now(self.ctx, sched.pk, now=NOW)["status"], "ok")
        self.assertEqual(self.spawned, [])


class AKilledRunDoesNotBlockTests(BackgroundBase):
    """The spawn did nothing: the run was claimed and nobody will ever finish it."""

    def setUp(self):
        super().setUp()
        self.sched = self.once()
        schedules.run_now(self.ctx, self.sched.pk, now=NOW)

    def test_within_a_minute_the_refusal_says_when_the_run_started(self):
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.run_now(self.ctx, self.sched.pk, now=NOW + seconds(30))
        self.assertIn("started at 09:04", str(caught.exception))
        self.assertIn("about a minute", str(caught.exception))
        self.assertEqual(len(self.spawned), 1)

    def test_after_about_a_minute_it_can_be_run_again(self):
        later = NOW + seconds(61)
        self.assertEqual(schedules.run_now(self.ctx, self.sched.pk, now=later)["status"], "started")
        self.assertEqual(len(self.spawned), 2)

    def test_it_stops_reading_as_running_after_about_a_minute(self):
        self.sched.refresh_from_db()
        self.assertEqual(schedules.running_since(self.sched, NOW + seconds(59)), NOW)
        self.assertIsNone(schedules.running_since(self.sched, NOW + seconds(61)))

    def test_the_tick_records_the_stopped_run(self):
        with self.assertLogs("origin.schedules", level="WARNING"):
            schedules.run_due(NOW + seconds(120))
        self.sched.refresh_from_db()
        self.assertEqual((self.sched.claimed_at, self.sched.heartbeat_at), (None, None))
        self.assertEqual((self.sched.last_status, self.sched.last_run_at), ("failed", NOW))
        self.assertEqual(self.sched.last_error, schedules.RUN_STOPPED_SENTENCE)
        self.assertEqual(self.sched.next_run_at, utc(2026, 11, 9, 5))  # still waits for its time
        self.assertEqual(self.sched.consecutive_failures, 0)

    def test_a_live_run_is_left_alone_by_the_tick(self):
        self.assertEqual(schedules.fail_stale_hand_runs(NOW + seconds(30)), 0)

    def test_a_claim_without_a_heartbeat_waits_the_old_time_and_names_it(self):
        MemberSchedule.objects.filter(pk=self.sched.pk).update(heartbeat_at=None)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.run_now(self.ctx, self.sched.pk, now=NOW + seconds(120))
        self.assertIn("started at 09:04", str(caught.exception))
        self.assertIn("again at 09:19", str(caught.exception))
        later = NOW + schedules.STALE_CLAIM + seconds(1)
        self.assertEqual(schedules.run_now(self.ctx, self.sched.pk, now=later)["status"], "started")


class TheTickAndTheHandRunTests(BackgroundBase):
    def test_the_tick_takes_over_a_due_run_whose_heartbeat_stopped_and_no_live_one(self):
        sched = self.make(job="ok", due_at=SCHEDULED, now=utc(2026, 11, 7))
        MemberSchedule.objects.filter(pk=sched.pk).update(
            claimed_at=NOW - seconds(90), heartbeat_at=NOW - seconds(20)
        )
        self.assertEqual(schedules.run_due(NOW).due, 0)
        MemberSchedule.objects.filter(pk=sched.pk).update(heartbeat_at=NOW - seconds(61))
        self.assertEqual(schedules.run_due(NOW).ran, 1)
        sched.refresh_from_db()
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (None, None))

    def test_a_recurring_schedule_is_claimed_too_and_keeps_its_next_run(self):
        sched = self.make(job="slow", due_at=utc(2030, 1, 1))
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        with self.assertRaises(schedules.ScheduleError):
            schedules.run_now(self.ctx, sched.pk, now=NOW + seconds(5))
        self.run_child()
        sched.refresh_from_db()
        self.assertEqual(sched.next_run_at, utc(2030, 1, 1))
        self.assertEqual((sched.last_status, sched.claimed_at), ("ok", None))


class StartingTheChildTests(BackgroundBase):
    @override_settings(SCHEDULE_RUN_NOW_MAX_PROCS=1)
    def test_past_the_cap_it_is_refused_and_not_left_claimed(self):
        first, second = self.once(), self.once(name="Two")
        schedules.run_now(self.ctx, first.pk, now=NOW)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.run_now(self.ctx, second.pk, now=NOW)
        self.assertEqual(str(caught.exception), schedule_runner.BUSY_SENTENCE)
        second.refresh_from_db()
        self.assertIsNone(second.claimed_at)

    def test_a_child_that_cannot_start_leaves_nothing_claimed(self):
        sched = self.once()
        with mock.patch.object(schedule_runner, "_spawn_process", side_effect=OSError("no")):
            with self.assertLogs("origin.schedules", level="WARNING"):
                with self.assertRaises(schedules.ScheduleError):
                    schedules.run_now(self.ctx, sched.pk, now=NOW)
        sched.refresh_from_db()
        self.assertIsNone(sched.claimed_at)


class TheChildTests(BackgroundBase):
    def start(self, job):
        register_job(Job("child", "Child", "x", job, unwrapped=True))
        sched = self.once(job="child")
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        return sched

    def test_a_job_that_raises_is_recorded_and_the_command_ends_normally(self):
        sched = self.start(self._boom)
        with self.assertLogs("origin.schedules", level="WARNING"):
            self.run_child()
        sched.refresh_from_db()
        self.assertEqual((sched.last_status, sched.last_error), ("failed", "RuntimeError"))
        self.assertIsNone(sched.claimed_at)

    def test_a_run_past_its_time_limit_says_so(self):
        def too_long(ctx, params):
            raise schedules.RunTimedOut

        sched = self.start(too_long)
        with self.assertLogs("origin.schedules", level="WARNING"):
            self.run_child()
        sched.refresh_from_db()
        self.assertEqual(
            (sched.last_status, sched.last_error), ("failed", schedules.RUN_TOO_LONG_SENTENCE)
        )
        self.assertIsNone(sched.claimed_at)


class RunRightAwayInTheBackgroundTests(BackgroundBase):
    def go(self, **over):
        fields = {"name": "Now", "job": "slow", "frequency": "once", "now": NOW}
        fields.update(over)
        return schedules.create_and_run(self.ctx, **fields)

    def test_a_slow_one_is_answered_started_and_keeps_its_row(self):
        sched, out = self.go()
        self.assertEqual(out["status"], "started")
        self.assertEqual(self.calls, [])
        self.assertEqual(sched.claimed_at, NOW)
        self.assertIsNone(sched.next_run_at)  # the tick never files it
        self.assertEqual(len(self.spawned), 1)

    def test_a_failed_one_is_replaced_by_the_next_try_under_the_same_name(self):
        first, _ = self.go()
        with self.assertLogs("origin.schedules", level="WARNING"):
            schedules.run_due(NOW + seconds(120))  # the child died
        second, _ = self.go(now=NOW + seconds(200))
        self.assertEqual(list(MemberSchedule.objects.values_list("pk", flat=True)), [second.pk])
        self.assertNotEqual(first.pk, second.pk)

    def test_one_that_ever_made_a_report_is_not_replaced(self):
        first, _ = self.go()
        MemberSchedule.objects.filter(pk=first.pk).update(
            claimed_at=None, last_status="failed", last_result={"title": "T", "items": 1}
        )
        with self.assertRaises(schedules.ScheduleError):
            self.go(now=NOW + seconds(200))
        self.assertTrue(MemberSchedule.objects.filter(pk=first.pk).exists())


class RestTests(BackgroundBase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.q = f"?team_id={self.team.team_id}"

    def test_run_answers_started_and_the_list_says_running(self):
        sched = self.make(job="slow")
        res = self.client.post(f"/api/v2/schedules/{sched.pk}/run/{self.q}")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["status"], "started")
        self.assertNotIn("started_at", res.data)
        row = self.client.get(f"/api/v2/schedules/{self.q}").data["schedules"]
        row = next(r for r in row if r["id"] == str(sched.pk))
        self.assertIs(row["running"], True)
        self.assertEqual(row["runningSince"], res.data["startedAt"])
        self.run_child()
        rows = self.client.get(f"/api/v2/schedules/{self.q}").data["schedules"]
        row = next(r for r in rows if r["id"] == str(sched.pk))
        self.assertIs(row["running"], False)
        # The run the page was told about is the one the row now records.
        self.assertEqual(row["lastRun"], res.data["startedAt"])

    def test_run_right_away_answers_started(self):
        body = {
            "team_id": str(self.team.team_id),
            "name": "Now",
            "job": "slow",
            "frequency": "once",
            "runNow": True,
        }
        res = self.client.post("/api/v2/schedules/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["run"]["status"], "started")
        self.assertIs(res.data["schedule"]["running"], True)

    def test_the_dashboards_list_says_running(self):
        sched = self.make(job="slow")
        MemberSchedule.objects.filter(pk=sched.pk).update(job="ai_dashboard")
        url = f"/api/v2/schedules/dashboards/{self.q}"
        self.assertIs(self.client.get(url).data["own"][0]["running"], False)
        schedules._claim_for_run_now(sched.pk, schedules.timezone.now())
        self.assertIs(self.client.get(url).data["own"][0]["running"], True)


class TheHeartbeatThreadTests(CheapFlushTransactionTestCase):
    """A real second thread on its own connection (a `TestCase`'s rows are invisible to it)."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="beater", email="beater@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Beat", team_email="beat@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        patcher = mock.patch.dict(JOBS, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen = []

    def test_the_heartbeat_moves_while_the_job_runs(self):
        def job(ctx, params):
            for _ in range(40):
                time.sleep(0.05)
                row = MemberSchedule.objects.get(user=self.user)
                self.seen.append(row.heartbeat_at)
                if len(set(self.seen)) >= 3:
                    break
            return {"title": "T", "sections": []}

        register_job(Job("ok", "OK", "Fine.", job, unwrapped=True))
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        sched = schedules.create(ctx, name="Beat", job="ok", frequency="daily", time_of_day="09:00")
        with override_settings(SCHEDULE_HEARTBEAT_EVERY_SECONDS=0.05):
            out = schedules.run_now(ctx, sched.pk)
        self.assertEqual(out["status"], "ok", out)
        self.assertGreaterEqual(len(set(self.seen)), 3, self.seen)
        sched.refresh_from_db()
        self.assertEqual((sched.claimed_at, sched.heartbeat_at), (None, None))

"""The schedule tools over MCP (MIP-1151): list, create, change or pause, delete, run now.

What matters, in the order tested:

  * a member can say "every weekday at 9, run what to do today" and see it back in words,
    with the next run in their own time zone;
  * the schedules are the member's own: another member's id is "not found", their list is
    theirs, and a read-only connection cannot change anything;
  * deleting needs the connection's destructive opt-in, but pausing does not (it is the
    undoable way to stop one);
  * "run now" delivers the report and is limited to once a minute, so a loop cannot flood
    the Inbox;
  * what the service refuses is said in the member's words, not as a crash.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import schedule_delivery, schedule_text, schedules
from origin.services.schedule_jobs import JOBS, Job
from origin.tests.test_mcp_endpoint import McpBase

TOOLS = (
    "list_my_schedules",
    "create_my_schedule",
    "update_my_schedule",
    "delete_my_schedule",
    "run_my_schedule",
)


def fake_job(ctx, params):
    return {
        "title": f"Your day ({params.get('day', 'today')})",
        "summary": "Two things.",
        "sections": [{"heading": "Due", "items": [{"text": "one"}, {"text": "two"}]}],
    }


class ScheduleToolBase(McpBase):
    def setUp(self):
        super().setUp()
        # These tests are about the page, not about the defaults (MIP-1178): the member was
        # seeded long ago, so a first list creates nothing.
        MemberScheduleSeed.objects.create(user=self.user, team=self.team)
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        patcher = mock.patch.dict(
            JOBS,
            {
                "fake_day": Job(
                    "fake_day",
                    "Fake day",
                    "A job for the tests.",
                    fake_job,
                    {"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
                )
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def text(self, tool, **args):
        text, is_error = self._tool_text(self._call(tool, args))
        return text, is_error

    def ok(self, tool, **args):
        text, is_error = self.text(tool, **args)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, tool, **args):
        text, is_error = self.text(tool, **args)
        self.assertTrue(is_error, text)
        return text

    def create(self, **over):
        args = {
            "name": "Morning",
            "job": "fake_day",
            "frequency": "weekdays",
            "time_of_day": "09:00",
        }
        args.update(over)
        return self.ok("create_my_schedule", **args)["schedule"]


class TestTheToolsAreListedAndMcpOnly(ScheduleToolBase):
    def test_they_exist_only_on_the_mcp_surface(self):
        for name in TOOLS:
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertNotIn(name, REGISTRY)

    def test_who_sees_which(self):
        def listed():
            return {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}

        self._key(scope=SCOPE_READ)
        self.assertEqual(set(TOOLS) & listed(), {"list_my_schedules"})
        self._key(scope=SCOPE_WRITE)
        self.assertEqual(set(TOOLS) & listed(), set(TOOLS) - {"delete_my_schedule"})
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertEqual(set(TOOLS) & listed(), set(TOOLS))

    def test_the_hints_say_which_call_changes_what(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        hints = {name: tools[name]["annotations"] for name in TOOLS}
        self.assertTrue(hints["list_my_schedules"]["readOnlyHint"])
        self.assertFalse(hints["create_my_schedule"]["destructiveHint"])
        self.assertTrue(hints["update_my_schedule"]["destructiveHint"])
        self.assertTrue(hints["delete_my_schedule"]["destructiveHint"])
        self.assertFalse(hints["run_my_schedule"]["destructiveHint"])


class TestCreateAndList(ScheduleToolBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE, allow_destructive=True)

    def test_a_member_creates_a_schedule_and_reads_it_back_in_words(self):
        row = self.create(name="What to do", frequency="weekly", weekdays=[0, 2])
        self.assertEqual(row["when"], "every Monday and Wednesday at 09:00")
        self.assertEqual(row["job_title"], "Fake day")
        self.assertTrue(row["enabled"])
        self.assertFalse(row["deliver_push"])
        stored = MemberSchedule.objects.get(pk=row["schedule_id"])
        self.assertEqual(
            (stored.user_id, str(stored.team_id)), (self.user.id, str(self.team.team_id))
        )
        self.assertEqual(stored.weekdays, [0, 2])
        # The next run is shown in the member's own zone, +09:00.
        self.assertTrue(row["next_run"].endswith("+09:00"), row["next_run"])
        self.assertTrue(row["next_run"][11:16] == "09:00")

        listed = self.ok("list_my_schedules")
        self.assertEqual([s["schedule_id"] for s in listed["schedules"]], [row["schedule_id"]])
        self.assertEqual(listed["time_zone"], "Asia/Tokyo")
        self.assertEqual(listed["limit"], schedules.MAX_SCHEDULES_PER_MEMBER)

    def test_the_list_says_what_each_job_does_and_takes(self):
        jobs = {j["job"]: j for j in self.ok("list_my_schedules")["jobs"]}
        self.assertEqual(jobs["fake_day"]["description"], "A job for the tests.")
        self.assertIn("day", jobs["fake_day"]["params"])

    def test_the_list_offers_the_suggested_schedules_the_member_does_not_have(self):
        with mock.patch.dict(
            JOBS,
            {
                "what_to_do_today": Job("what_to_do_today", "Today", "d", fake_job, {}),
                "what_you_did": Job(
                    "what_you_did",
                    "Did",
                    "d",
                    fake_job,
                    {"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
                ),
            },
        ):
            suggested = self.ok("list_my_schedules")["suggested"]
            self.assertEqual([s["key"] for s in suggested], ["morning_plan", "evening_recap"])
            self.create(name="Mine", job="what_to_do_today", frequency="daily")
            suggested = self.ok("list_my_schedules")["suggested"]
            self.assertEqual([s["key"] for s in suggested], ["evening_recap"])

    def test_every_recurrence_reads_naturally(self):
        cases = [
            ({"frequency": "daily"}, "every day at 09:00"),
            ({"frequency": "weekdays"}, "every Monday to Friday at 09:00"),
            ({"frequency": "weekly", "weekdays": [4]}, "every Friday at 09:00"),
            (
                {"frequency": "weekly", "weekdays": [0, 2, 4]},
                "every Monday, Wednesday and Friday at 09:00",
            ),
            ({"frequency": "monthly", "month_day": 1}, "on the 1st of every month at 09:00"),
            ({"frequency": "monthly", "month_day": 22}, "on the 22nd of every month at 09:00"),
            ({"frequency": "monthly", "month_day": 11}, "on the 11th of every month at 09:00"),
        ]
        for over, expected in cases:
            self.assertEqual(self.create(**over)["when"], expected)
        once = self.create(
            frequency="once", run_date=(timezone.now() + timedelta(days=3)).date().isoformat()
        )
        self.assertTrue(once["when"].startswith("once, on 20"))

    def test_job_parameters_are_checked_and_kept(self):
        row = self.create(params={"day": "yesterday"})
        self.assertEqual(row["params"], {"day": "yesterday"})
        self.assertIn(
            "day",
            self.refused(
                "create_my_schedule",
                name="x",
                job="fake_day",
                frequency="daily",
                time_of_day="09:00",
                params={"day": "tomorrow"},
            ).lower(),
        )

    def test_what_the_service_refuses_is_said_in_words(self):
        base = {"name": "x", "frequency": "daily", "time_of_day": "09:00"}
        self.assertIn("no job called", self.refused("create_my_schedule", job="nope", **base))
        self.assertIn(
            "09:30",
            self.refused("create_my_schedule", job="fake_day", **{**base, "time_of_day": "9am"}),
        )
        self.assertIn(
            "weekday",
            self.refused("create_my_schedule", job="fake_day", **{**base, "frequency": "weekly"}),
        )
        self.assertIn(
            "no parameter",
            self.refused("create_my_schedule", job="fake_day", params={"hours": 3}, **base),
        )
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_there_is_a_limit_per_member(self):
        for n in range(schedules.MAX_SCHEDULES_PER_MEMBER):
            self.create(name=f"s{n}")
        self.assertIn(
            "at most",
            self.refused(
                "create_my_schedule",
                name="one more",
                job="fake_day",
                frequency="daily",
                time_of_day="09:00",
            ),
        )

    def test_push_is_off_unless_asked_for(self):
        self.assertFalse(self.create()["deliver_push"])
        self.assertTrue(self.create(name="p", deliver_push=True)["deliver_push"])

    def test_a_read_only_key_cannot_create(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self.text(
            "create_my_schedule", name="x", job="fake_day", frequency="daily", time_of_day="09:00"
        )
        self.assertTrue(is_error, text)
        self.assertEqual(MemberSchedule.objects.count(), 0)


class TestOwnerOnly(ScheduleToolBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.mine = self.create(name="Mine")["schedule_id"]
        self.theirs = schedules.create(
            self.ctx2(),
            name="Theirs",
            job="fake_day",
            params={},
            frequency="daily",
            time_of_day="10:00",
        )

    def ctx2(self):
        from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415

        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))

    def test_my_list_holds_only_mine(self):
        names = [s["name"] for s in self.ok("list_my_schedules")["schedules"]]
        self.assertEqual(names, ["Mine"])

    def test_someone_elses_schedule_is_not_found_for_every_tool(self):
        other = str(self.theirs.id)
        for name, args in (
            ("update_my_schedule", {"name": "stolen"}),
            ("update_my_schedule", {"enabled": False}),
            ("delete_my_schedule", {}),
            ("run_my_schedule", {}),
        ):
            self.assertIn("not found", self.refused(name, schedule_id=other, **args).lower())
        self.theirs.refresh_from_db()
        self.assertEqual((self.theirs.name, self.theirs.enabled), ("Theirs", True))

    def test_a_made_up_id_is_the_same_not_found(self):
        self.assertIn(
            "not found", self.refused("delete_my_schedule", schedule_id="nonsense").lower()
        )


class TestChangePauseDelete(ScheduleToolBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.sid = self.create()["schedule_id"]

    def test_changing_the_time_moves_the_next_run(self):
        before = MemberSchedule.objects.get(pk=self.sid).next_run_at
        row = self.ok("update_my_schedule", schedule_id=self.sid, time_of_day="17:30")["schedule"]
        self.assertEqual(row["time_of_day"], "17:30")
        self.assertEqual(row["next_run"][11:16], "17:30")
        self.assertNotEqual(MemberSchedule.objects.get(pk=self.sid).next_run_at, before)

    def test_pausing_needs_no_delete_opt_in_and_resuming_starts_again(self):
        paused = self.ok("update_my_schedule", schedule_id=self.sid, enabled=False)["schedule"]
        self.assertFalse(paused["enabled"])
        self.assertIsNone(paused["next_run"])
        self.assertEqual(paused["paused_reason"], "paused by the member")
        resumed = self.ok("update_my_schedule", schedule_id=self.sid, enabled=True)["schedule"]
        self.assertTrue(resumed["enabled"])
        self.assertIsNotNone(resumed["next_run"])
        self.assertNotIn("paused_reason", resumed)

    def test_resuming_forgets_a_pause_from_failures(self):
        MemberSchedule.objects.filter(pk=self.sid).update(
            enabled=False, paused_reason="failures", consecutive_failures=3, next_run_at=None
        )
        row = self.ok("update_my_schedule", schedule_id=self.sid, enabled=True)["schedule"]
        self.assertTrue(row["enabled"])
        self.assertEqual(MemberSchedule.objects.get(pk=self.sid).consecutive_failures, 0)

    def test_changing_nothing_is_said(self):
        self.assertIn("Nothing to change", self.refused("update_my_schedule", schedule_id=self.sid))

    def test_a_bad_change_leaves_the_schedule_as_it_was(self):
        self.refused("update_my_schedule", schedule_id=self.sid, time_of_day="25:00")
        self.assertEqual(MemberSchedule.objects.get(pk=self.sid).time_of_day.hour, 9)

    def test_deleting_needs_the_destructive_opt_in(self):
        text = self.refused("delete_my_schedule", schedule_id=self.sid)
        self.assertTrue(MemberSchedule.objects.filter(pk=self.sid).exists(), text)
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertTrue(self.ok("delete_my_schedule", schedule_id=self.sid)["deleted"])
        self.assertFalse(MemberSchedule.objects.filter(pk=self.sid).exists())

    def test_deleting_twice_says_not_found(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.ok("delete_my_schedule", schedule_id=self.sid)
        self.assertIn("not found", self.refused("delete_my_schedule", schedule_id=self.sid).lower())


class TestRunNow(ScheduleToolBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.sid = self.create()["schedule_id"]

    def test_it_runs_the_job_and_hands_the_report_to_delivery(self):
        with mock.patch.object(
            schedule_delivery, "deliver", wraps=schedule_delivery.deliver
        ) as spy:
            out = self.ok("run_my_schedule", schedule_id=self.sid)
        self.assertEqual((out["status"], out["items"]), ("ok", 2))
        self.assertEqual(out["title"], "Your day (today)")
        spy.assert_called_once()
        row = self.ok("list_my_schedules")["schedules"][0]
        self.assertEqual(row["last_status"], "ok")
        self.assertEqual(row["last_result"], {"title": "Your day (today)", "items": 2})

    def test_it_does_not_move_the_next_run(self):
        before = MemberSchedule.objects.get(pk=self.sid).next_run_at
        self.ok("run_my_schedule", schedule_id=self.sid)
        self.assertEqual(MemberSchedule.objects.get(pk=self.sid).next_run_at, before)

    def test_a_second_run_within_a_minute_is_refused_and_delivers_nothing(self):
        self.ok("run_my_schedule", schedule_id=self.sid)
        with mock.patch.object(schedule_delivery, "deliver") as deliver:
            text = self.refused("run_my_schedule", schedule_id=self.sid)
        self.assertIn("Try again in", text)
        deliver.assert_not_called()

    def test_after_a_minute_it_runs_again(self):
        self.ok("run_my_schedule", schedule_id=self.sid)
        MemberSchedule.objects.filter(pk=self.sid).update(
            last_run_at=timezone.now() - schedules.RUN_NOW_COOLDOWN - timedelta(seconds=1)
        )
        self.assertEqual(self.ok("run_my_schedule", schedule_id=self.sid)["status"], "ok")

    def test_a_failing_job_is_reported_in_words_not_as_a_crash(self):
        def boom(ctx, params):
            raise RuntimeError("secret detail that must not leak")

        with mock.patch.dict(JOBS, {"fake_day": Job("fake_day", "x", "y", boom, {})}):
            with self.assertLogs("origin.schedules", level="WARNING"):
                out = self.ok("run_my_schedule", schedule_id=self.sid)
        self.assertEqual(out["status"], "failed")
        self.assertNotIn("secret detail", json.dumps(out))
        row = self.ok("list_my_schedules")["schedules"][0]
        self.assertEqual(row["last_error"], "RuntimeError")
        self.assertNotIn("secret detail", json.dumps(row))


class TestWording(ScheduleToolBase):
    def test_a_schedule_whose_job_was_removed_says_so(self):
        self._key(scope=SCOPE_WRITE)
        sid = self.create()["schedule_id"]
        with mock.patch.dict(JOBS, clear=True):
            row = self.ok("list_my_schedules")["schedules"][0]
        self.assertIn("no longer exists", row["problem"])
        self.assertEqual(row["schedule_id"], sid)

    def test_the_text_helpers_do_not_query(self):
        sched = MemberSchedule(
            name="n",
            job="fake_day",
            frequency="daily",
            time_of_day=timezone.now().time().replace(second=0, microsecond=0),
        )
        with self.assertNumQueries(0):
            schedule_text.describe_recurrence(sched)

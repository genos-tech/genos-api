"""Member schedules (MIP-1147): the next-run arithmetic, the member's own CRUD, the tick.

Expected instants in the arithmetic tests are worked out by hand from the zone rules (the
offsets are in the comments), never by asking the code under test.
"""

import json
import types
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase

from origin.models.common.ops_models import CronHeartbeat
from origin.models.common.schedule_models import MemberSchedule
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.models import AiSpendEvent
from origin.services import schedule_delivery, schedules
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.services.schedule_jobs import JOBS, Job, register_job
from origin.tests.test_base import BaseAPITestCase

UTC = timezone.utc
TOKYO = ZoneInfo("Asia/Tokyo")  # UTC+9 all year
LONDON = ZoneInfo("Europe/London")  # GMT = UTC+0, BST = UTC+1
NEW_YORK = ZoneInfo("America/New_York")  # EST = UTC-5, EDT = UTC-4


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def rec(frequency, at="09:00", **kw):
    hour, minute = at.split(":")
    return schedules.Recurrence(frequency, time(int(hour), int(minute)), **kw)


class NextRunAfterTests(SimpleTestCase):
    def test_daily_is_strictly_after(self):
        r = rec("daily")
        # 09:00 JST is 00:00 UTC.
        self.assertEqual(
            schedules.next_run_after(r, utc(2026, 11, 6, 23, 59), TOKYO), utc(2026, 11, 7)
        )
        self.assertEqual(schedules.next_run_after(r, utc(2026, 11, 7), TOKYO), utc(2026, 11, 8))
        self.assertEqual(
            schedules.next_run_after(r, utc(2026, 11, 7, 0, 1), TOKYO), utc(2026, 11, 8)
        )

    def test_the_local_date_not_the_utc_date_decides_the_day(self):
        # 23:00 UTC on Nov 6 is 08:00 on Nov 7 in Tokyo: today's 09:00 is still ahead.
        self.assertEqual(
            schedules.next_run_after(rec("daily"), utc(2026, 11, 6, 23), TOKYO), utc(2026, 11, 7)
        )

    def test_weekdays_skips_the_weekend(self):
        r = rec("weekdays")
        friday = utc(2026, 11, 6, 0, 1)  # Fri 09:01 JST
        self.assertEqual(date(2026, 11, 6).weekday(), 4)
        self.assertEqual(schedules.next_run_after(r, friday, TOKYO), utc(2026, 11, 9))  # Monday
        self.assertEqual(schedules.next_run_after(r, utc(2026, 11, 7), TOKYO), utc(2026, 11, 9))

    def test_weekly_with_several_days(self):
        r = rec("weekly", weekdays=(0, 2))  # Monday, Wednesday
        saturday = utc(2026, 11, 7)
        monday = schedules.next_run_after(r, saturday, TOKYO)
        self.assertEqual(monday, utc(2026, 11, 9))
        wednesday = schedules.next_run_after(r, monday, TOKYO)
        self.assertEqual(wednesday, utc(2026, 11, 11))
        self.assertEqual(schedules.next_run_after(r, wednesday, TOKYO), utc(2026, 11, 16))

    def test_weekly_on_sunday_is_six(self):
        self.assertEqual(
            schedules.next_run_after(rec("weekly", weekdays=(6,)), utc(2026, 11, 7), TOKYO),
            utc(2026, 11, 8),
        )

    def test_monthly_day_31_runs_on_each_months_last_day(self):
        r = rec("monthly", month_day=31)
        jan = utc(2027, 1, 31)
        feb = schedules.next_run_after(r, jan, TOKYO)
        self.assertEqual(feb, utc(2027, 2, 28))
        mar = schedules.next_run_after(r, feb, TOKYO)
        self.assertEqual(mar, utc(2027, 3, 31))
        self.assertEqual(schedules.next_run_after(r, mar, TOKYO), utc(2027, 4, 30))

    def test_monthly_in_a_leap_year_uses_the_29th(self):
        r = rec("monthly", month_day=31)
        self.assertEqual(schedules.next_run_after(r, utc(2028, 1, 31), TOKYO), utc(2028, 2, 29))

    def test_monthly_a_plain_day_waits_for_next_month_when_passed(self):
        r = rec("monthly", month_day=15)
        self.assertEqual(schedules.next_run_after(r, utc(2026, 11, 15), TOKYO), utc(2026, 12, 15))
        self.assertEqual(schedules.next_run_after(r, utc(2026, 11, 14), TOKYO), utc(2026, 11, 15))

    def test_once_runs_if_ahead_and_never_if_passed(self):
        r = rec("once", run_date=date(2026, 12, 1))
        self.assertEqual(schedules.next_run_after(r, utc(2026, 11, 7), TOKYO), utc(2026, 12, 1))
        self.assertIsNone(schedules.next_run_after(r, utc(2026, 12, 1), TOKYO))
        self.assertIsNone(schedules.next_run_after(r, utc(2027, 1, 1), TOKYO))

    def test_accepts_a_dict_and_a_clock_string(self):
        fields = {"frequency": "daily", "time_of_day": "09:00"}
        self.assertEqual(
            schedules.next_run_after(fields, utc(2026, 11, 7), TOKYO), utc(2026, 11, 8)
        )

    def test_no_dst_zone_keeps_its_offset_all_year(self):
        r = rec("daily")
        self.assertEqual(schedules.next_run_after(r, utc(2027, 3, 20), TOKYO), utc(2027, 3, 21))
        self.assertEqual(schedules.next_run_after(r, utc(2027, 7, 20), TOKYO), utc(2027, 7, 21))

    # London: clocks go forward 2027-03-28 01:00 GMT -> 02:00 BST, back 2026-10-25 02:00 BST -> 01:00 GMT.
    def test_london_nine_oclock_follows_the_clock_change(self):
        r = rec("daily")
        saturday = schedules.next_run_after(r, utc(2027, 3, 27, 8), LONDON)
        self.assertEqual(saturday, utc(2027, 3, 27, 9))  # GMT
        self.assertEqual(schedules.next_run_after(r, saturday, LONDON), utc(2027, 3, 28, 8))  # BST

    def test_london_spring_forward_a_missing_time_runs_at_the_first_valid_moment(self):
        r = rec("daily", "01:30")  # 01:30 does not exist on 2027-03-28
        before = utc(2027, 3, 27, 1, 30)  # the 27th's 01:30 GMT
        # The first valid moment is 02:00 BST = 01:00 UTC.
        self.assertEqual(schedules.next_run_after(r, before, LONDON), utc(2027, 3, 28, 1, 0))
        # ...and once it has run, the next one is the 29th at 01:30 BST = 00:30 UTC.
        self.assertEqual(
            schedules.next_run_after(r, utc(2027, 3, 28, 1, 0), LONDON), utc(2027, 3, 29, 0, 30)
        )

    def test_london_fall_back_an_ambiguous_time_runs_once(self):
        r = rec("daily", "01:30")  # 01:30 happens twice on 2026-10-25
        first = schedules.next_run_after(r, utc(2026, 10, 24, 0, 30), LONDON)
        self.assertEqual(first, utc(2026, 10, 25, 0, 30))  # the BST one
        # The repeated 01:30 (GMT, 01:30 UTC) is NOT a second run: next is the 26th.
        self.assertEqual(schedules.next_run_after(r, first, LONDON), utc(2026, 10, 26, 1, 30))

    # New York: forward 2027-03-14 02:00 EST -> 03:00 EDT, back 2026-11-01 02:00 EDT -> 01:00 EST.
    def test_new_york_spring_forward_a_missing_time_runs_at_three(self):
        r = rec("daily", "02:30")
        before = utc(2027, 3, 13, 7, 30)  # the 13th's 02:30 EST
        self.assertEqual(schedules.next_run_after(r, before, NEW_YORK), utc(2027, 3, 14, 7, 0))
        self.assertEqual(
            schedules.next_run_after(r, utc(2027, 3, 14, 7, 0), NEW_YORK), utc(2027, 3, 15, 6, 30)
        )

    def test_new_york_fall_back_an_ambiguous_time_runs_once(self):
        r = rec("daily", "01:30")
        first = schedules.next_run_after(r, utc(2026, 10, 31, 5, 30), NEW_YORK)
        self.assertEqual(first, utc(2026, 11, 1, 5, 30))  # EDT
        self.assertEqual(
            schedules.next_run_after(r, first, NEW_YORK), utc(2026, 11, 2, 6, 30)
        )  # EST

    def test_new_york_nine_oclock_follows_the_clock_change(self):
        r = rec("daily")
        self.assertEqual(
            schedules.next_run_after(r, utc(2027, 3, 13, 14), NEW_YORK), utc(2027, 3, 14, 13)
        )

    def test_a_once_at_a_missing_time_runs_at_the_first_valid_moment(self):
        r = rec("once", "02:30", run_date=date(2027, 3, 14))
        self.assertEqual(
            schedules.next_run_after(r, utc(2027, 3, 1), NEW_YORK), utc(2027, 3, 14, 7)
        )

    def test_weekly_across_the_clock_change(self):
        r = rec("weekly", weekdays=(6,))  # Sundays 09:00 London
        before = utc(2027, 3, 21, 9)  # Sunday, GMT
        self.assertEqual(schedules.next_run_after(r, before, LONDON), utc(2027, 3, 28, 8))  # BST


class ScheduleBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.calls = []
        patcher = mock.patch.dict(JOBS, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        register_job(Job("ok", "OK", "Fine.", self._ok))
        register_job(Job("boom", "Boom", "Raises.", self._boom))
        register_job(Job("sqlboom", "SQL boom", "Breaks the transaction.", self._sqlboom))
        register_job(Job("badshape", "Bad", "Wrong shape.", lambda ctx, params: "nope"))
        register_job(
            Job(
                "weekly_report",
                "Weekly",
                "Has parameters.",
                self._ok,
                {
                    "properties": {
                        "days": {"type": "integer", "minimum": 1, "maximum": 31},
                        "scope": {"type": "string", "enum": ["mine", "team"]},
                    },
                    "required": ["scope"],
                },
            )
        )

    def _ok(self, ctx, params):
        self.calls.append((ctx.user_id, ctx.team_id, dict(params)))
        return {
            "title": "Your day",
            "summary": "two things",
            "sections": [
                {"heading": "Open", "items": [{"text": "a"}, {"text": "b", "link": "/x"}]},
            ],
        }

    def _boom(self, ctx, params):
        self.calls.append("boom")
        raise RuntimeError("secret text from a message body")

    def _sqlboom(self, ctx, params):
        self.calls.append("sqlboom")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1/0")

    def make(self, name="Daily", job="ok", due_at=None, ctx=None, **kw):
        kw.setdefault("frequency", "daily")
        kw.setdefault("time_of_day", "09:00")
        sched = schedules.create(ctx or self.ctx, name=name, job=job, **kw)
        if due_at is not None:
            MemberSchedule.objects.filter(pk=sched.pk).update(next_run_at=due_at)
        sched.refresh_from_db()
        return sched


NOW = utc(2026, 11, 9, 0, 4)  # Monday 09:04 in Tokyo
SCHEDULED = utc(2026, 11, 9, 0, 0)  # Monday 09:00 in Tokyo


class CreateTests(ScheduleBase):
    def test_create_computes_the_first_run_in_the_members_zone(self):
        sched = schedules.create(
            self.ctx,
            name="  Morning\n plan ",
            job="ok",
            frequency="daily",
            time_of_day="09:00",
            now=utc(2026, 11, 7, 3),
        )
        self.assertEqual(sched.name, "Morning plan")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 8))  # 09:00 JST Nov 8
        self.assertEqual(sched.tz_used, "Asia/Tokyo")
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.user_id, self.user.id)
        self.assertEqual(str(sched.team_id), str(self.team.team_id))

    def test_a_member_with_no_zone_runs_in_the_server_zone(self):
        self.user.timezone = None
        self.user.save()
        sched = schedules.create(
            self.ctx,
            name="n",
            job="ok",
            frequency="daily",
            time_of_day="09:00",
            now=utc(2026, 11, 7),
        )
        self.assertEqual(sched.tz_used, "UTC")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 7, 9))

    def test_weekly_days_are_sorted_and_deduplicated_and_unused_fields_dropped(self):
        sched = self.make(
            frequency="weekly", weekdays=[2, 0, 2], month_day=5, run_date="2030-01-01"
        )
        self.assertEqual(sched.weekdays, [0, 2])
        self.assertIsNone(sched.month_day)
        self.assertIsNone(sched.run_date)

    def test_time_may_be_a_time_object(self):
        sched = self.make(time_of_day=time(13, 5))
        self.assertEqual(sched.time_of_day, time(13, 5))

    def _bad(self, message_part, **kw):
        kw.setdefault("name", "n")
        kw.setdefault("job", "ok")
        kw.setdefault("frequency", "daily")
        kw.setdefault("time_of_day", "09:00")
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.create(self.ctx, **kw)
        self.assertIn(message_part, str(caught.exception))
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_validation_messages_say_what_is_wrong(self):
        self._bad("needs a name", name="   ")
        self._bad("needs a name", name=None)
        self._bad("too long", name="x" * 101)
        self._bad("no job called 'nope'", job="nope")
        self._bad("Available jobs", job="nope")
        self._bad("`frequency` must be one of", frequency="hourly")
        self._bad("at least one weekday", frequency="weekly")
        self._bad("at least one weekday", frequency="weekly", weekdays=[])
        self._bad("from 0 (Monday) to 6", frequency="weekly", weekdays=[7])
        self._bad("from 0 (Monday) to 6", frequency="weekly", weekdays=[True])
        self._bad("`month_day`", frequency="monthly")
        self._bad("`month_day`", frequency="monthly", month_day=0)
        self._bad("`month_day`", frequency="monthly", month_day=32)
        self._bad("`run_date`", frequency="once")
        self._bad("2026-12-01", frequency="once", run_date="tomorrow")
        self._bad("already passed", frequency="once", run_date="2020-01-01")
        self._bad("09:30", time_of_day="25:00")
        self._bad("09:30", time_of_day="9am")
        self._bad("whole minute", time_of_day=time(9, 30, 15))
        self._bad("needs a time of day", time_of_day=None)

    def test_params_are_checked_against_the_jobs_schema(self):
        self._bad("must be an object", params=[1])
        self._bad("needs the parameter 'scope'", job="weekly_report", params={})
        self._bad("no parameter 'color'", job="weekly_report", params={"scope": "mine", "color": 1})
        self._bad(
            "must be of type integer", job="weekly_report", params={"scope": "mine", "days": "7"}
        )
        self._bad(
            "must be of type integer", job="weekly_report", params={"scope": "mine", "days": True}
        )
        self._bad("at most 31", job="weekly_report", params={"scope": "mine", "days": 99})
        self._bad("at least 1", job="weekly_report", params={"scope": "mine", "days": 0})
        self._bad("one of mine, team", job="weekly_report", params={"scope": "everyone"})

    def test_params_that_pass_are_stored(self):
        sched = self.make(job="weekly_report", params={"scope": "team", "days": 7})
        self.assertEqual(sched.params, {"scope": "team", "days": 7})

    def test_oversized_params_are_refused(self):
        register_job(
            Job(
                "loose",
                "Loose",
                "Takes anything named blob.",
                self._ok,
                {"properties": {"blob": {}}},
            )
        )
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.create(
                self.ctx,
                name="n",
                job="loose",
                params={"blob": "x" * 2100},
                frequency="daily",
                time_of_day="09:00",
            )
        self.assertIn("too large", str(caught.exception))

    def test_the_cap_is_twenty_per_member_and_other_members_are_unaffected(self):
        self.assertEqual(schedules.MAX_SCHEDULES_PER_MEMBER, 20)
        for i in range(20):
            self.make(name=f"s{i}")
        with self.assertRaises(schedules.ScheduleError) as caught:
            self.make(name="one too many")
        self.assertIn("20", str(caught.exception))
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 20)
        self.make(name="mine", ctx=self.ctx2)
        # A deleted one frees a place.
        schedules.delete(self.ctx, MemberSchedule.objects.filter(user=self.user).first().pk)
        self.make(name="fits now")

    def test_the_cap_counts_the_members_other_teams_too(self):
        other = TeamMaster.objects.create(
            team_name="Two", team_email="two@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        ctx_other = ToolContext(team_id=str(other.team_id), user_id=str(self.user.id))
        for i in range(20):
            self.make(name=f"s{i}", ctx=ctx_other)
        with self.assertRaises(schedules.ScheduleError):
            self.make(name="third place")

    def test_a_non_member_cannot_create(self):
        stranger = TeamMaster.objects.create(
            team_name="Else", team_email="else@example.com", owner=self.user2
        )
        ctx = ToolContext(team_id=str(stranger.team_id), user_id=str(self.user.id))
        with self.assertRaises(schedules.ScheduleError) as caught:
            self.make(ctx=ctx)
        self.assertIn("not a member", str(caught.exception))

    def test_a_removed_member_cannot_create(self):
        TeamMembers.objects.filter(attendee=self.user).update(is_deleted=True)
        with self.assertRaises(schedules.ScheduleError):
            self.make()


class ScopingTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.sched = self.make()

    def test_a_strangers_id_is_not_found_for_every_operation(self):
        pk = self.sched.pk
        for call in (
            lambda: schedules.update(self.ctx2, pk, name="x"),
            lambda: schedules.set_enabled(self.ctx2, pk, False),
            lambda: schedules.delete(self.ctx2, pk),
            lambda: schedules.run_now(self.ctx2, pk),
        ):
            with self.assertRaises(schedules.ScheduleNotFound):
                call()
        self.assertTrue(MemberSchedule.objects.filter(pk=pk, name="Daily", enabled=True).exists())
        self.assertEqual(self.calls, [])

    def test_another_team_of_the_same_member_cannot_reach_it(self):
        other = TeamMaster.objects.create(
            team_name="Two", team_email="two@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.user.id))
        with self.assertRaises(schedules.ScheduleNotFound):
            schedules.update(ctx, self.sched.pk, name="x")
        self.assertEqual(schedules.list_schedules(ctx), [])

    def test_a_malformed_id_is_just_not_found(self):
        for bad in ("nope", "", None, 123, "1" * 40):
            for call in (
                lambda: schedules.update(self.ctx, bad, name="x"),
                lambda: schedules.set_enabled(self.ctx, bad, True),
                lambda: schedules.delete(self.ctx, bad),
                lambda: schedules.run_now(self.ctx, bad),
            ):
                with self.assertRaises(schedules.ScheduleNotFound):
                    call()

    def test_list_shows_only_the_members_own(self):
        self.make(name="theirs", ctx=self.ctx2)
        self.assertEqual([s.name for s in schedules.list_schedules(self.ctx)], ["Daily"])

    def test_delete_removes_it(self):
        schedules.delete(self.ctx, self.sched.pk)
        self.assertFalse(MemberSchedule.objects.filter(pk=self.sched.pk).exists())
        with self.assertRaises(schedules.ScheduleNotFound):
            schedules.delete(self.ctx, self.sched.pk)


class UpdateTests(ScheduleBase):
    def test_a_name_change_leaves_the_next_run_alone(self):
        sched = self.make(due_at=utc(2026, 12, 1))
        schedules.update(self.ctx, sched.pk, name="Renamed")
        sched.refresh_from_db()
        self.assertEqual(sched.name, "Renamed")
        self.assertEqual(sched.next_run_at, utc(2026, 12, 1))

    def test_a_recurrence_change_recomputes_the_next_run(self):
        sched = self.make()
        schedules.update(
            self.ctx,
            sched.pk,
            frequency="weekly",
            weekdays=[0],
            time_of_day="10:00",
            now=utc(2026, 11, 7),
        )
        sched.refresh_from_db()
        self.assertEqual(sched.next_run_at, utc(2026, 11, 9, 1))  # Monday 10:00 JST
        self.assertEqual(sched.weekdays, [0])

    def test_weekly_to_daily_needs_no_tidying(self):
        sched = self.make(frequency="weekly", weekdays=[1])
        schedules.update(self.ctx, sched.pk, frequency="daily")
        sched.refresh_from_db()
        self.assertEqual(sched.weekdays, [])

    def test_weekly_without_days_is_refused_on_update_too(self):
        sched = self.make()
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.pk, frequency="weekly")

    def test_a_job_change_is_checked_against_the_new_jobs_params(self):
        sched = self.make()
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.pk, job="weekly_report")
        schedules.update(self.ctx, sched.pk, job="weekly_report", params={"scope": "mine"})
        sched.refresh_from_db()
        self.assertEqual((sched.job, sched.params), ("weekly_report", {"scope": "mine"}))

    def test_unknown_fields_cannot_be_changed(self):
        sched = self.make()
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.update(self.ctx, sched.pk, next_run_at=utc(2026, 1, 1), enabled=False)
        self.assertIn("enabled", str(caught.exception))

    def test_a_disabled_schedule_stays_disabled_when_edited(self):
        sched = self.make()
        schedules.set_enabled(self.ctx, sched.pk, False)
        schedules.update(self.ctx, sched.pk, time_of_day="10:00")
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)
        self.assertIsNone(sched.next_run_at)

    def test_pause_and_resume(self):
        sched = self.make()
        paused = schedules.set_enabled(self.ctx, sched.pk, False)
        self.assertEqual(
            (paused.enabled, paused.paused_reason, paused.next_run_at), (False, "manual", None)
        )
        MemberSchedule.objects.filter(pk=sched.pk).update(consecutive_failures=2)
        resumed = schedules.set_enabled(self.ctx, sched.pk, True, now=utc(2026, 11, 7, 3))
        self.assertEqual(
            (resumed.enabled, resumed.paused_reason, resumed.consecutive_failures),
            (True, "", 0),
        )
        self.assertEqual(resumed.next_run_at, utc(2026, 11, 8))

    def test_resuming_a_finished_one_off_says_why_not(self):
        sched = self.make(frequency="once", run_date="2026-12-01", now=utc(2026, 11, 7))
        schedules.set_enabled(self.ctx, sched.pk, False)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.set_enabled(self.ctx, sched.pk, True, now=utc(2026, 12, 2))
        self.assertIn("already passed", str(caught.exception))


class RunDueTests(ScheduleBase):
    def test_a_due_schedule_runs_as_the_member_and_moves_to_its_next_time(self):
        sched = self.make(job="weekly_report", params={"scope": "mine"}, due_at=SCHEDULED)
        summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual((summary.due, summary.ran, summary.failed), (1, 1, 0))
        self.assertEqual(
            self.calls, [(str(self.user.id), str(self.team.team_id), {"scope": "mine"})]
        )
        self.assertEqual(sched.last_status, "ok")
        self.assertEqual(sched.last_run_at, NOW)
        self.assertEqual(sched.last_error, "")
        self.assertEqual(sched.last_result, {"title": "Your day", "items": 2})
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10))  # from 09:00 Monday, not from 09:04
        self.assertIsNone(sched.claimed_at)
        self.assertEqual(sched.consecutive_failures, 0)

    def test_a_run_costs_nothing(self):
        self.make(due_at=SCHEDULED)
        schedules.run_due(NOW)
        self.assertEqual(AiSpendEvent.objects.count(), 0)

    def test_the_next_run_is_the_next_scheduled_time_not_a_day_after_the_run(self):
        # A run two hours late is still due at the same hour tomorrow.
        sched = self.make(due_at=SCHEDULED)
        schedules.run_due(SCHEDULED + timedelta(hours=2))
        sched.refresh_from_db()
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10))

    def test_nothing_due_does_nothing(self):
        sched = self.make(due_at=NOW + timedelta(minutes=1))
        summary = schedules.run_due(NOW)
        self.assertEqual(summary.due, 0)
        self.assertEqual(self.calls, [])
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "")

    def test_no_schedules_at_all(self):
        summary = schedules.run_due(NOW)
        self.assertEqual(summary, schedules.RunSummary())

    def test_paused_and_disabled_are_not_run(self):
        sched = self.make(due_at=SCHEDULED)
        MemberSchedule.objects.filter(pk=sched.pk).update(enabled=False)
        self.make(name="no next", due_at=None)
        MemberSchedule.objects.filter(name="no next").update(next_run_at=None)
        self.assertEqual(schedules.run_due(NOW).due, 0)
        self.assertEqual(self.calls, [])

    def test_oldest_first(self):
        self.make(name="b", params={}, due_at=SCHEDULED + timedelta(minutes=2))
        self.make(name="a", due_at=SCHEDULED)
        self.make(name="c", due_at=SCHEDULED + timedelta(minutes=3))
        order = []
        real = schedules._process

        def spy(row, now, summary):
            order.append(row.name)
            return real(row, now, summary)

        with mock.patch.object(schedules, "_process", spy):
            schedules.run_due(NOW)
        self.assertEqual(order, ["a", "b", "c"])

    # ── the claim ──

    def test_a_claim_can_be_taken_once(self):
        sched = self.make(due_at=SCHEDULED)
        self.assertTrue(schedules._claim(sched.pk, NOW))
        self.assertFalse(schedules._claim(sched.pk, NOW))
        self.assertFalse(schedules._claim(sched.pk, NOW + timedelta(minutes=14)))

    def test_a_stale_claim_is_taken_over(self):
        sched = self.make(due_at=SCHEDULED)
        MemberSchedule.objects.filter(pk=sched.pk).update(claimed_at=NOW - timedelta(minutes=16))
        self.assertTrue(schedules._claim(sched.pk, NOW))

    def test_a_stale_claim_does_not_strand_the_schedule(self):
        sched = self.make(due_at=SCHEDULED)
        MemberSchedule.objects.filter(pk=sched.pk).update(claimed_at=NOW - timedelta(minutes=16))
        summary = schedules.run_due(NOW)
        self.assertEqual(summary.ran, 1)

    def test_a_fresh_claim_blocks_the_run(self):
        sched = self.make(due_at=SCHEDULED)
        MemberSchedule.objects.filter(pk=sched.pk).update(claimed_at=NOW - timedelta(minutes=5))
        self.assertEqual(schedules.run_due(NOW).due, 0)
        self.assertEqual(self.calls, [])

    def test_a_claim_is_refused_when_it_is_no_longer_due_or_enabled(self):
        sched = self.make(due_at=NOW + timedelta(seconds=1))
        self.assertFalse(schedules._claim(sched.pk, NOW))
        MemberSchedule.objects.filter(pk=sched.pk).update(next_run_at=SCHEDULED, enabled=False)
        self.assertFalse(schedules._claim(sched.pk, NOW))

    def test_two_overlapping_ticks_run_it_once(self):
        # The second tick starts while the first is inside the job.
        sched = self.make(due_at=SCHEDULED)
        inner = []

        def job(ctx, params):
            inner.append(schedules.run_due(NOW + timedelta(minutes=1)))
            return self._ok(ctx, params)

        JOBS["ok"] = Job("ok", "OK", "Fine.", job)
        outer = schedules.run_due(NOW)
        self.assertEqual(outer.ran, 1)
        self.assertEqual(inner[0].due, 0)
        self.assertEqual(inner[0].ran, 0)
        self.assertEqual(len(self.calls), 1)
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "ok")

    def test_a_second_tick_after_the_first_finished_finds_nothing_due(self):
        self.make(due_at=SCHEDULED)
        schedules.run_due(NOW)
        schedules.run_due(NOW + timedelta(minutes=5))
        self.assertEqual(len(self.calls), 1)

    def test_a_schedule_the_tick_listed_but_lost_the_claim_for_is_counted_not_run(self):
        sched = self.make(due_at=SCHEDULED)
        row = schedules.due_schedules(now=NOW)[0]
        MemberSchedule.objects.filter(pk=sched.pk).update(claimed_at=NOW)
        summary = schedules.RunSummary()
        schedules._process(row, NOW, summary)
        self.assertEqual(summary.claimed_elsewhere, 1)
        self.assertEqual(self.calls, [])

    def test_an_edit_made_during_the_run_wins(self):
        sched = self.make(due_at=SCHEDULED)

        def job(ctx, params):
            schedules.update(self.ctx, sched.pk, name="Edited", time_of_day="15:00", now=NOW)
            return self._ok(ctx, params)

        JOBS["ok"] = Job("ok", "OK", "Fine.", job)
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(sched.name, "Edited")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 9, 6))  # 15:00 JST, set by the edit
        self.assertIsNone(sched.claimed_at)

    def test_a_schedule_deleted_during_the_run_is_gone_and_the_tick_goes_on(self):
        sched = self.make(due_at=SCHEDULED)
        self.make(name="later", due_at=SCHEDULED + timedelta(minutes=1))

        def job(ctx, params):
            MemberSchedule.objects.filter(pk=sched.pk).delete()
            return self._ok(ctx, params)

        JOBS["ok"] = Job("ok", "OK", "Fine.", job)
        with self.assertNoLogs("origin", level="ERROR"):
            summary = schedules.run_due(NOW)
        self.assertEqual(summary.ran, 2)
        self.assertEqual(MemberSchedule.objects.count(), 1)

    # ── lateness ──

    def test_a_run_more_than_the_grace_late_is_skipped_and_recorded(self):
        sched = self.make(due_at=SCHEDULED)
        now = SCHEDULED + schedules.LATE_GRACE + timedelta(seconds=1)  # 12:00:01 JST
        summary = schedules.run_due(now)
        sched.refresh_from_db()
        self.assertEqual((summary.skipped_late, summary.ran), (1, 0))
        self.assertEqual(self.calls, [])
        self.assertEqual(sched.last_status, "skipped_late")
        self.assertIsNone(sched.last_run_at)
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10))
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.consecutive_failures, 0)

    def test_exactly_the_grace_late_still_runs(self):
        self.make(due_at=SCHEDULED)
        schedules.run_due(SCHEDULED + schedules.LATE_GRACE)
        self.assertEqual(len(self.calls), 1)

    def test_a_long_outage_gives_one_outcome_then_the_next_future_time(self):
        sched = self.make(due_at=SCHEDULED)
        now = SCHEDULED + timedelta(days=3, hours=5)  # Thursday 14:00 JST
        summary = schedules.run_due(now)
        sched.refresh_from_db()
        self.assertEqual(summary.skipped_late, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(sched.next_run_at, utc(2026, 11, 13))  # Friday 09:00 JST
        self.assertEqual(schedules.run_due(now + timedelta(minutes=5)).due, 0)

    def test_an_occurrence_missed_by_a_little_still_runs_on_the_next_tick(self):
        # Outage of 25 hours: Monday's is too late, Tuesday's is only 30 minutes late.
        sched = self.make(due_at=SCHEDULED)
        now = SCHEDULED + timedelta(hours=24, minutes=30)
        schedules.run_due(now)
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "skipped_late")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10))
        schedules.run_due(now + timedelta(minutes=5))
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "ok")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 11))
        self.assertEqual(len(self.calls), 1)

    def test_once_runs_then_has_no_next_time(self):
        sched = self.make(
            frequency="once", run_date="2026-11-09", due_at=SCHEDULED, now=utc(2026, 11, 7)
        )
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "ok")
        self.assertIsNone(sched.next_run_at)
        self.assertEqual(schedules.run_due(NOW + timedelta(days=1)).due, 0)
        self.assertEqual(len(self.calls), 1)

    def test_a_once_that_was_missed_is_skipped_and_finished(self):
        sched = self.make(
            frequency="once", run_date="2026-11-09", due_at=SCHEDULED, now=utc(2026, 11, 7)
        )
        schedules.run_due(SCHEDULED + timedelta(days=2))
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "skipped_late")
        self.assertIsNone(sched.next_run_at)
        self.assertEqual(self.calls, [])

    # ── access ──

    def test_a_member_who_left_the_team_is_skipped_and_paused(self):
        sched = self.make(due_at=SCHEDULED)
        TeamMembers.objects.filter(attendee=self.user, team=self.team).update(is_deleted=True)
        summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(summary.skipped_access, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(
            (sched.last_status, sched.enabled, sched.paused_reason, sched.next_run_at),
            ("skipped_access", False, "left_team", None),
        )
        self.assertIsNone(sched.claimed_at)
        self.assertEqual(schedules.run_due(NOW + timedelta(days=1)).due, 0)

    def test_a_deleted_team_is_the_same_as_leaving_it(self):
        sched = self.make(due_at=SCHEDULED)
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(sched.paused_reason, "left_team")

    def test_an_inactive_account_is_skipped_and_paused(self):
        sched = self.make(due_at=SCHEDULED)
        self.user.is_active = False
        self.user.save()
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(
            (sched.last_status, sched.enabled, sched.paused_reason),
            ("skipped_access", False, "inactive"),
        )
        self.assertEqual(self.calls, [])

    def test_a_deleted_account_is_skipped_and_paused(self):
        sched = self.make(due_at=SCHEDULED)
        self.user.is_deleted = True
        self.user.save()
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(sched.paused_reason, "inactive")
        self.assertEqual(self.calls, [])

    def test_someone_elses_lost_access_does_not_touch_other_schedules(self):
        self.make(name="mine", due_at=SCHEDULED)
        self.make(name="theirs", due_at=SCHEDULED, ctx=self.ctx2)
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=True)
        summary = schedules.run_due(NOW)
        self.assertEqual((summary.ran, summary.skipped_access), (1, 1))
        self.assertTrue(MemberSchedule.objects.get(name="mine").enabled)

    # ── failures ──

    def test_a_failing_job_is_recorded_without_quoting_it_and_logs_a_warning(self):
        sched = self.make(job="boom", due_at=SCHEDULED)
        with self.assertLogs("origin.schedules", level="WARNING") as logs:
            summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual((summary.failed, summary.ran), (1, 0))
        self.assertEqual(sched.last_status, "failed")
        self.assertEqual(sched.last_error, "RuntimeError")
        self.assertEqual(sched.consecutive_failures, 1)
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10))
        self.assertTrue(all(r.levelname == "WARNING" for r in logs.records))
        self.assertNotIn("secret text", sched.last_error)

    def test_a_failure_never_logs_at_error(self):
        self.make(job="boom", due_at=SCHEDULED)
        with self.assertNoLogs("origin", level="ERROR"):
            schedules.run_due(NOW)

    def test_three_failures_in_a_row_pause_the_schedule(self):
        sched = self.make(job="boom", due_at=SCHEDULED)
        for day in range(3):
            self.assertTrue(MemberSchedule.objects.get(pk=sched.pk).enabled, day)
            schedules.run_due(NOW + timedelta(days=day))
        sched.refresh_from_db()
        self.assertEqual(sched.consecutive_failures, 3)
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.paused_reason, "failures")
        self.assertIsNone(sched.next_run_at)
        self.assertEqual(schedules.run_due(NOW + timedelta(days=9)).due, 0)

    def test_two_failures_do_not_pause(self):
        sched = self.make(job="boom", due_at=SCHEDULED)
        for day in range(2):
            schedules.run_due(NOW + timedelta(days=day))
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.consecutive_failures, 2)

    def test_a_success_resets_the_count(self):
        sched = self.make(job="boom", due_at=SCHEDULED)
        schedules.run_due(NOW)
        schedules.run_due(NOW + timedelta(days=1))
        MemberSchedule.objects.filter(pk=sched.pk).update(job="ok")
        schedules.run_due(NOW + timedelta(days=2))
        sched.refresh_from_db()
        self.assertEqual((sched.last_status, sched.consecutive_failures), ("ok", 0))
        MemberSchedule.objects.filter(pk=sched.pk).update(job="boom")
        for day in (3, 4):
            schedules.run_due(NOW + timedelta(days=day))
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)  # two since the reset, not four

    def test_one_failure_leaves_the_others_untouched_and_the_tick_goes_on(self):
        bad = self.make(name="bad", job="boom", due_at=SCHEDULED)
        good = self.make(name="good", due_at=SCHEDULED + timedelta(minutes=1))
        summary = schedules.run_due(NOW)
        self.assertEqual((summary.failed, summary.ran), (1, 1))
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(good.last_status, "ok")
        self.assertEqual(good.consecutive_failures, 0)
        self.assertEqual(bad.consecutive_failures, 1)

    def test_a_job_that_breaks_the_database_transaction_does_not_break_the_next(self):
        self.make(name="sql", job="sqlboom", due_at=SCHEDULED)
        good = self.make(name="good", due_at=SCHEDULED + timedelta(minutes=1))
        with self.assertNoLogs("origin", level="ERROR"):
            summary = schedules.run_due(NOW)
        good.refresh_from_db()
        self.assertEqual((summary.failed, summary.ran), (1, 1))
        self.assertEqual(good.last_status, "ok")
        self.assertEqual(MemberSchedule.objects.get(name="sql").last_error, "DataError")

    def test_an_unknown_job_key_is_a_failed_run_not_a_crash(self):
        sched = self.make(due_at=SCHEDULED)
        MemberSchedule.objects.filter(pk=sched.pk).update(job="removed_in_a_deploy")
        with self.assertNoLogs("origin", level="ERROR"):
            summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(sched.last_status, "failed")
        self.assertIn("no longer exists", sched.last_error)

    def test_a_malformed_report_is_a_failed_run(self):
        sched = self.make(job="badshape", due_at=SCHEDULED)
        schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual(sched.last_status, "failed")
        self.assertIn("wrong shape", sched.last_error)

    def test_a_delivery_failure_is_a_failed_run(self):
        sched = self.make(due_at=SCHEDULED)
        with mock.patch.object(schedule_delivery, "deliver", side_effect=ValueError("inbox down")):
            schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual((sched.last_status, sched.last_error), ("failed", "ValueError"))

    def test_a_bookkeeping_fault_logs_an_error_and_the_tick_goes_on(self):
        self.make(name="a", due_at=SCHEDULED)
        self.make(name="b", due_at=SCHEDULED + timedelta(minutes=1))
        real = schedules._process
        seen = []

        def flaky(row, now, summary):
            seen.append(row.name)
            if row.name == "a":
                raise RuntimeError("db gone")
            return real(row, now, summary)

        with mock.patch.object(schedules, "_process", flaky):
            with self.assertLogs("origin.schedules", level="ERROR"):
                summary = schedules.run_due(NOW)
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(summary.ran, 1)

    # ── time zones ──

    def test_a_zone_change_recomputes_before_deciding_what_is_due(self):
        sched = self.make(due_at=SCHEDULED)  # 09:00 Tokyo
        self.user.timezone = "Europe/London"
        self.user.save()
        summary = schedules.run_due(NOW)  # 00:04 UTC: not 09:00 in London
        sched.refresh_from_db()
        self.assertEqual((summary.rezoned, summary.ran), (1, 0))
        self.assertEqual(self.calls, [])
        self.assertEqual(sched.tz_used, "Europe/London")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 9, 9))  # GMT in November

    def test_a_zone_change_that_makes_it_due_runs_it_in_the_same_tick(self):
        sched = self.make(due_at=SCHEDULED)
        self.user.timezone = "Asia/Singapore"  # UTC+8: 09:00 is 01:00 UTC
        self.user.save()
        now = utc(2026, 11, 9, 1, 2)
        summary = schedules.run_due(now)
        sched.refresh_from_db()
        self.assertEqual((summary.rezoned, summary.ran), (1, 1))
        self.assertEqual(sched.tz_used, "Asia/Singapore")
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10, 1))

    def test_a_zone_change_does_not_resurrect_a_run_that_is_long_past(self):
        sched = self.make(due_at=SCHEDULED)
        self.user.timezone = "Asia/Singapore"
        self.user.save()
        schedules.run_due(utc(2026, 11, 9, 8))  # 16:00 SGT: today's 09:00 is 7 hours gone
        sched.refresh_from_db()
        self.assertEqual(self.calls, [])
        self.assertEqual(sched.next_run_at, utc(2026, 11, 10, 1))

    def test_an_unchanged_zone_is_not_recomputed(self):
        self.make(due_at=SCHEDULED)
        self.assertEqual(schedules.run_due(NOW).rezoned, 0)

    # ── bounds ──

    def test_the_limit_bounds_one_tick(self):
        for i in range(3):
            self.make(name=f"s{i}", due_at=SCHEDULED + timedelta(minutes=i))
        summary = schedules.run_due(NOW, limit=2)
        self.assertEqual((summary.due, summary.ran), (2, 2))
        self.assertEqual(schedules.run_due(NOW).ran, 1)

    def test_a_spent_budget_stops_picking_new_ones_and_the_rest_wait(self):
        for i in range(3):
            self.make(name=f"s{i}", due_at=SCHEDULED + timedelta(minutes=i))
        ticks = iter([0.0, 1.0, 11.0, 12.0, 13.0])  # start, after #1 is fine, then 11 s in
        clock = types.SimpleNamespace(monotonic=lambda: next(ticks))
        with mock.patch.object(schedules, "time", clock):
            summary = schedules.run_due(NOW, budget_seconds=10)
        self.assertEqual((summary.ran, summary.deferred), (1, 2))
        self.assertEqual(schedules.run_due(NOW).ran, 2)

    def test_a_zero_budget_runs_nothing(self):
        self.make(due_at=SCHEDULED)
        summary = schedules.run_due(NOW, budget_seconds=0)
        self.assertEqual((summary.ran, summary.deferred), (0, 1))
        self.assertEqual(self.calls, [])


class RunNowTests(ScheduleBase):
    def test_runs_whatever_the_clock_says_and_keeps_the_next_run(self):
        sched = self.make(due_at=utc(2030, 1, 1))
        result = schedules.run_now(self.ctx, sched.pk, now=NOW)
        sched.refresh_from_db()
        self.assertEqual(result, {"status": "ok", "error": "", "title": "Your day", "items": 2})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sched.next_run_at, utc(2030, 1, 1))
        self.assertEqual((sched.last_status, sched.last_run_at), ("ok", NOW))

    def test_a_failure_is_reported_and_does_not_count_toward_the_pause(self):
        sched = self.make(job="boom")
        MemberSchedule.objects.filter(pk=sched.pk).update(consecutive_failures=2)
        with self.assertLogs("origin.schedules", level="WARNING"):
            result = schedules.run_now(self.ctx, sched.pk)
        sched.refresh_from_db()
        self.assertEqual((result["status"], result["error"]), ("failed", "RuntimeError"))
        self.assertEqual(sched.consecutive_failures, 2)
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.last_status, "failed")

    def test_runs_a_paused_schedule_too(self):
        sched = self.make()
        schedules.set_enabled(self.ctx, sched.pk, False)
        self.assertEqual(schedules.run_now(self.ctx, sched.pk)["status"], "ok")

    def test_a_member_who_left_cannot_run_it(self):
        sched = self.make()
        TeamMembers.objects.filter(attendee=self.user).update(is_deleted=True)
        with self.assertRaises(schedules.ScheduleError):
            schedules.run_now(self.ctx, sched.pk)
        self.assertEqual(self.calls, [])


class DeliveryAndRegistryTests(ScheduleBase):
    def test_the_default_hook_keeps_a_summary_and_logs_without_the_title(self):
        sched = self.make()
        report = {
            "title": "Private title",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}],
        }
        with self.assertLogs("origin.schedules", level="INFO") as logs:
            schedule_delivery.deliver(sched, report)
        sched.refresh_from_db()
        self.assertEqual(sched.last_result, {"title": "Private title", "items": 3})
        self.assertNotIn("Private title", "\n".join(logs.output))
        self.assertIn("items=3", "\n".join(logs.output))

    def test_a_duplicate_key_is_refused(self):
        with self.assertRaises(ValueError):
            register_job(Job("ok", "again", "x", self._ok))

    def test_a_bad_key_is_refused(self):
        for key in ("", " x", "x" * 65):
            with self.assertRaises(ValueError):
                register_job(Job(key, "t", "d", self._ok))

    def test_there_are_no_real_jobs_in_this_step(self):
        with mock.patch.dict(JOBS, clear=True):
            self.assertEqual(JOBS, {})
            with self.assertRaises(schedules.ScheduleError) as caught:
                schedules.create(
                    self.ctx, name="n", job="x", frequency="daily", time_of_day="09:00"
                )
        self.assertIn("none yet", str(caught.exception))


class CommandTests(ScheduleBase):
    def call(self, *args):
        out = StringIO()
        call_command("schedule_tick", *args, stdout=out)
        return out.getvalue()

    def test_nothing_due_prints_a_summary_and_does_nothing(self):
        out = self.call()
        self.assertIn("0 ran, 0 failed", out)
        self.assertEqual(self.calls, [])

    def test_runs_what_is_due(self):
        sched = self.make(due_at=utc(2020, 1, 1))
        MemberSchedule.objects.filter(pk=sched.pk).update(
            next_run_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        out = self.call()
        self.assertIn("1 ran", out)
        self.assertEqual(len(self.calls), 1)

    def test_a_failing_member_job_does_not_fail_the_cron(self):
        sched = self.make(job="boom")
        MemberSchedule.objects.filter(pk=sched.pk).update(
            next_run_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        try:
            # `_beat` calls close_old_connections(), which would end the test's connection.
            with mock.patch("origin.services.ops_alerts.close_old_connections"):
                out = self.call("--heartbeat", "schedule-tick")
        except CommandError as exc:  # pragma: no cover - the failure being tested
            self.fail(f"a member's failing job failed the cron: {exc}")
        self.assertIn("1 failed", out)
        beat = CronHeartbeat.objects.get(name="schedule-tick")
        self.assertIsNotNone(beat.last_succeeded_at)

    def test_a_bookkeeping_fault_does_fail_the_cron(self):
        sched = self.make()
        MemberSchedule.objects.filter(pk=sched.pk).update(
            next_run_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        with mock.patch.object(schedules, "_process", side_effect=RuntimeError("db gone")):
            with self.assertRaises(CommandError):
                self.call()

    def test_dry_run_runs_nothing_and_writes_nothing(self):
        due = self.make(name="due", due_at=utc(2020, 1, 1))
        zone_changed = self.make(name="zone", due_at=utc(2020, 1, 1), ctx=self.ctx2)
        self.user2.timezone = "Europe/London"  # tz_used is still the server zone
        self.user2.save()
        before = list(MemberSchedule.objects.order_by("name").values())
        out = self.call("--dry-run")
        self.assertIn("2 schedule(s) due", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(MemberSchedule.objects.order_by("name").values()), before)
        self.assertIsNone(MemberSchedule.objects.get(pk=due.pk).claimed_at)
        self.assertEqual(MemberSchedule.objects.get(pk=zone_changed.pk).tz_used, "UTC")


class ErasureAndExportTests(ScheduleBase):
    def test_deleting_the_account_erases_their_schedules_and_only_theirs(self):
        self.make(name="mine")
        self.make(name="theirs", ctx=self.ctx2)
        delete_account(self.user2)
        self.assertEqual(MemberSchedule.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 1)

    def test_a_deleted_member_has_nothing_left_for_the_tick_to_run(self):
        self.make(name="theirs", ctx=self.ctx2, due_at=SCHEDULED)
        delete_account(self.user2)
        self.assertEqual(schedules.run_due(NOW).due, 0)

    def test_the_export_includes_their_schedules_and_not_anyone_elses(self):
        sched = self.make(
            name="Monday",
            frequency="weekly",
            weekdays=[0],
            params={},
            due_at=SCHEDULED,
            time_of_day="09:30",
        )
        schedules.run_due(NOW + timedelta(minutes=30))
        self.make(name="not exported", ctx=self.ctx2)
        doc = build_export(self.user)
        self.assertEqual(doc["counts"]["schedules"], 1)
        row = doc["schedules"][0]
        self.assertEqual(row["name"], "Monday")
        self.assertEqual(row["job"], "ok")
        self.assertEqual(row["frequency"], "weekly")
        self.assertEqual(row["weekdays"], [0])
        self.assertEqual(row["time_of_day"], "09:30")
        self.assertEqual(row["team_id"], str(self.team.team_id))
        self.assertEqual(row["last_status"], "ok")
        self.assertEqual(row["last_result"], {"title": "Your day", "items": 2})
        self.assertNotIn("not exported", json.dumps(doc))
        self.assertEqual(sched.pk, MemberSchedule.objects.get(name="Monday").pk)

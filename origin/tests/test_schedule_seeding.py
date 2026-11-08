"""The suggested schedules are created for every member, once (MIP-1178).

What matters, each pinned by a test that fails if the guarantee is removed:

  * a member is seeded ONCE: the marker keeps a second call, a later list and the tick away,
    even when the member has no schedules left, so a default they deleted never returns;
  * only in the member's default team (the earliest live membership), so a member of two
    teams does not get two reports per team;
  * ordinary schedules: push off, the member's own zone, the cap of 20 respected (what fits
    is made and the marker is still set), nothing duplicated when they already have one;
  * only active, live members of the team; the tick skips dormant, inactive, deleted and
    demo accounts, seeds at most a batch per pass, and one member's failure is a WARNING
    that does not stop the rest nor fail the cron;
  * the marker goes with the account and is in the export.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone as dj_timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_defaults, schedules
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp.secretary.list_my_schedules import _list as mcp_list

LIST = "/api/v2/schedules/"
UTC = timezone.utc
# 10:00 in Tokyo, a Saturday.
NOW = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)


def names(user):
    return sorted(MemberSchedule.objects.filter(user=user).values_list("name", flat=True))


class SeedBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.team_id = str(self.team.team_id)
        self.ctx = ToolContext(team_id=self.team_id, user_id=str(self.user.id))
        self._n = 0

    def another_team(self, owner=None, joined_days_after=1, member=None):
        """A second team the member joined `joined_days_after` days after the first."""
        team = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=owner or self.user
        )
        row = TeamMembers.objects.create(team=team, attendee=member or self.user)
        TeamMembers.objects.filter(pk=row.pk).update(
            ts_joined_at=dj_timezone.now() + timedelta(days=joined_days_after)
        )
        return team

    def member(self, token_age_days=1, **fields):
        """A new member of the test team; a refresh token issued `token_age_days` ago (None:
        never signed in)."""
        self._n += 1
        user = CustomUser.objects.create_user(
            username=f"seed{self._n}", email=f"seed{self._n}@example.com", password="x", **fields
        )
        TeamMembers.objects.create(team=self.team, attendee=user)
        if token_age_days is not None:
            self.sign_in(user, token_age_days)
        return user

    @staticmethod
    def sign_in(user, age_days):
        RefreshToken.for_user(user)
        OutstandingToken.objects.filter(user=user).update(
            created_at=dj_timezone.now() - timedelta(days=age_days)
        )

    def tick(self):
        out = StringIO()
        with mock.patch("origin.services.ops_alerts.close_old_connections"):
            call_command("schedule_tick", "--heartbeat", "schedule-tick", stdout=out)
        return out.getvalue()


class TestSeedDefaults(SeedBase):
    def test_a_member_gets_the_two_ordinary_schedules_in_their_own_zone(self):
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.assertEqual([s.name for s in made], ["Morning plan", "Evening recap"])
        morning, evening = made
        self.assertEqual((morning.job, morning.params), ("what_to_do_today", {}))
        self.assertEqual((evening.job, evening.params), ("what_you_did", {"day": "today"}))
        for sched in made:
            self.assertEqual(
                (sched.frequency, sched.enabled, sched.paused_reason), ("daily", True, "")
            )
            self.assertFalse(sched.deliver_push, "push stays off")
            self.assertEqual((sched.user_id, str(sched.team_id)), (self.user.id, self.team_id))
            self.assertEqual(sched.tz_used, "Asia/Tokyo")
        # 09:00 JST is 00:00 UTC and has passed (it is 10:00 JST): tomorrow. 17:00 JST is
        # 08:00 UTC, still ahead today.
        self.assertEqual(morning.next_run_at, datetime(2026, 11, 8, 0, 0, tzinfo=UTC))
        self.assertEqual(evening.next_run_at, datetime(2026, 11, 7, 8, 0, tzinfo=UTC))
        self.assertEqual(MemberScheduleSeed.objects.filter(user=self.user).count(), 1)

    def test_a_second_call_does_nothing(self):
        schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        again = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.assertEqual(again, [])
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 2)
        self.assertEqual(MemberScheduleSeed.objects.filter(user=self.user).count(), 1)

    def test_the_marker_is_unique_per_member_and_team(self):
        MemberScheduleSeed.objects.create(user=self.user, team=self.team)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MemberScheduleSeed.objects.create(user=self.user, team=self.team)
        self.assertEqual(MemberScheduleSeed._meta.db_table, "origin_memberscheduleseed")

    def test_a_default_the_member_deleted_never_comes_back(self):
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        for sched in made:
            schedules.delete(self.ctx, sched.pk)
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 0)
        # Not by a second call, not by the page, not by Claude's list, not by the tick.
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team), [])
        self.assertEqual(schedule_defaults.seed_if_default_team(self.user, self.team_id), [])
        self.sign_in(self.user, 1)
        self.tick()
        self.authenticate()
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(res.data["schedules"], [])
        self.assertEqual(mcp_list({}, self.ctx)["schedules"], [])
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 0)
        # ... but the page still OFFERS them: a passive one-click re-add.
        self.assertEqual(
            [s["key"] for s in res.data["suggestions"]], ["morning_plan", "evening_recap"]
        )

    def test_a_paused_default_stays_paused_and_is_not_duplicated(self):
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        schedules.set_enabled(self.ctx, made[0].pk, False)
        schedule_defaults.seed_defaults(self.user, self.team)
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 2)
        self.assertFalse(MemberSchedule.objects.get(pk=made[0].pk).enabled)

    def test_one_the_member_already_has_is_not_copied_but_the_marker_is_set(self):
        schedules.create(
            self.ctx,
            name="My own",
            job="what_to_do_today",
            params={},
            frequency="daily",
            time_of_day="09:00",
        )
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.assertEqual([s.name for s in made], ["Evening recap"])
        self.assertEqual(names(self.user), ["Evening recap", "My own"])
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())

    def test_at_the_cap_what_fits_is_made_and_the_marker_is_still_set(self):
        for i in range(schedules.MAX_SCHEDULES_PER_MEMBER - 1):
            schedules.create(
                self.ctx,
                name=f"s{i}",
                job="what_is_left",
                params={},
                frequency="weekly",
                weekdays=[i % 7],
                time_of_day=f"08:{i:02d}",
            )
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.assertEqual([s.name for s in made], ["Morning plan"])
        self.assertEqual(
            MemberSchedule.objects.filter(user=self.user).count(),
            schedules.MAX_SCHEDULES_PER_MEMBER,
        )
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())

    def test_full_up_makes_none_and_is_not_retried(self):
        for i in range(schedules.MAX_SCHEDULES_PER_MEMBER):
            schedules.create(
                self.ctx,
                name=f"s{i}",
                job="what_is_left",
                params={},
                frequency="weekly",
                weekdays=[i % 7],
                time_of_day=f"08:{i:02d}",
            )
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())
        schedules.delete(self.ctx, MemberSchedule.objects.filter(user=self.user).first().pk)
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])

    def test_not_for_an_inactive_deleted_demo_or_non_member(self):
        inactive = self.member(is_active=False)
        deleted = self.member(is_deleted=True)
        demo = self.member(is_demo=True)
        outsider = CustomUser.objects.create_user(
            username="out", email="out@example.com", password="x"
        )
        for user in (inactive, deleted, demo, outsider):
            self.assertEqual(
                schedule_defaults.seed_defaults(user, self.team, now=NOW), [], user.username
            )
            self.assertFalse(MemberScheduleSeed.objects.filter(user=user).exists())
            self.assertFalse(MemberSchedule.objects.filter(user=user).exists())

    def test_not_for_a_member_who_left_the_team(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])
        self.assertFalse(MemberScheduleSeed.objects.exists())

    def test_a_job_that_is_gone_is_not_created_but_the_marker_is_set(self):
        with mock.patch.dict(schedule_defaults.JOBS, {}, clear=True):
            self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())


class TestDefaultTeam(SeedBase):
    def test_it_is_the_earliest_live_membership(self):
        second = self.another_team()
        self.assertEqual(schedule_defaults.default_team_id(self.user), self.team_id)
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        self.assertEqual(schedule_defaults.default_team_id(self.user), str(second.team_id))
        TeamMaster.objects.filter(pk=second.pk).update(is_deleted=True)
        self.assertIsNone(schedule_defaults.default_team_id(self.user))

    def test_the_first_list_in_another_team_creates_nothing(self):
        second = self.another_team()
        self.authenticate()
        res = self.client.get(f"{LIST}?team_id={second.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["schedules"], [])
        self.assertEqual(len(res.data["suggestions"]), 2)
        self.assertFalse(MemberScheduleSeed.objects.exists())
        # The default team still seeds afterwards.
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(len(res.data["schedules"]), 2)

    def test_two_teams_never_make_four_schedules(self):
        second = self.another_team()
        self.authenticate()
        for team_id in (self.team_id, str(second.team_id), self.team_id, str(second.team_id)):
            self.client.get(f"{LIST}?team_id={team_id}")
        self.sign_in(self.user, 1)
        self.tick()
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 2)
        self.assertEqual(
            set(MemberSchedule.objects.values_list("team_id", flat=True)), {self.team.team_id}
        )

    def test_a_member_who_lost_their_first_team_is_not_seeded_again_elsewhere(self):
        second = self.another_team()
        schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        MemberSchedule.objects.filter(user=self.user).delete()
        TeamMembers.objects.filter(team=self.team, attendee=self.user).update(is_deleted=True)
        self.assertEqual(schedule_defaults.default_team_id(self.user), str(second.team_id))
        self.assertEqual(schedule_defaults.seed_defaults(self.user, second, now=NOW), [])
        self.assertEqual(schedule_defaults.seed_if_default_team(self.user, str(second.team_id)), [])
        self.assertEqual(schedule_defaults.pending_members(now=NOW), [])

    def test_the_tick_seeds_the_default_team_only(self):
        second = self.another_team()
        self.sign_in(self.user, 1)
        self.tick()
        self.assertEqual(
            set(MemberSchedule.objects.filter(user=self.user).values_list("team_id", flat=True)),
            {self.team.team_id},
        )
        self.assertFalse(MemberSchedule.objects.filter(team=second).exists())


class TestLazySeeding(SeedBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_the_first_list_creates_them_and_a_second_creates_nothing_more(self):
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            [s["name"] for s in res.data["schedules"]], ["Morning plan", "Evening recap"]
        )
        self.assertEqual(res.data["suggestions"], [], "they have them: nothing left to suggest")
        self.assertFalse(any(s["deliverPush"] for s in res.data["schedules"]))
        self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 2)

    def test_the_mcp_list_is_read_only_and_only_offers_them(self):
        out = mcp_list({}, self.ctx)
        self.assertEqual(out["schedules"], [])
        self.assertEqual([s["key"] for s in out["suggested"]], ["morning_plan", "evening_recap"])
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 0)

    def test_someone_who_is_not_in_the_team_gets_nothing_made(self):
        stranger = CustomUser.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        self.authenticate(stranger)
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(res.data["schedules"], [])
        self.assertFalse(MemberScheduleSeed.objects.exists())
        self.assertFalse(MemberSchedule.objects.filter(user=stranger).exists())

    def test_a_database_error_in_the_checks_does_not_poison_the_request(self):
        def broken(user):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM origin_no_such_table")

        with mock.patch.object(schedule_defaults, "default_team_id", side_effect=broken):
            with self.assertLogs("origin.schedules", level="WARNING"):
                res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["schedules"], [])

    def test_a_seeding_fault_never_breaks_the_list(self):
        with mock.patch.object(schedule_defaults, "_seed", side_effect=RuntimeError("db")):
            with self.assertLogs("origin.schedules", level="WARNING") as logs:
                res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["schedules"], [])
        self.assertIn("schedule_seed_failed", "\n".join(logs.output))
        # Nothing was marked, so the next list tries again.
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        self.assertEqual(len(res.data["schedules"]), 2)


class TestTickSeeding(SeedBase):
    def test_a_recently_signed_in_member_is_seeded_and_counted_in_the_summary(self):
        self.sign_in(self.user, 2)
        out = self.tick()
        self.assertIn("seeded 1 member(s) with 2 schedule(s), 0 seeding failed", out)
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 2)
        self.assertIn("seeded 0 member(s) with 0 schedule(s)", self.tick())

    def test_dormant_inactive_deleted_demo_teamless_and_never_signed_in_are_skipped(self):
        dormant = self.member(token_age_days=31)
        never = self.member(token_age_days=None)
        inactive = self.member(is_active=False)
        deleted = self.member(is_deleted=True)
        demo = self.member(is_demo=True)
        teamless = self.member()
        TeamMembers.objects.filter(attendee=teamless).delete()
        already = self.member()
        MemberScheduleSeed.objects.create(user=already, team=self.team)
        fresh = self.member(token_age_days=29)
        out = self.tick()
        self.assertIn("seeded 1 member(s)", out)
        for user in (dormant, never, inactive, deleted, demo, teamless, already):
            self.assertFalse(MemberSchedule.objects.filter(user=user).exists(), user.username)
        self.assertEqual(MemberSchedule.objects.filter(user=fresh).count(), 2)

    def test_members_who_can_never_be_seeded_do_not_take_up_the_batch(self):
        # `_seed` leaves no marker for these, so if the query let them through they would be
        # picked again every tick and starve everyone behind them.
        for fields in ({"is_active": False}, {"is_deleted": True}, {"is_demo": True}):
            for _ in range(2):
                self.member(**fields)
        for _ in range(3):
            user = self.member()
            TeamMembers.objects.filter(attendee=user).delete()
        real = self.member()
        self.assertEqual([u.pk for u in schedule_defaults.pending_members(limit=100)], [real.pk])

    def test_at_most_a_batch_a_pass_and_the_rest_next_time(self):
        for _ in range(schedule_defaults.SEED_BATCH + 5):
            self.member()
        out = self.tick()
        self.assertIn(f"seeded {schedule_defaults.SEED_BATCH} member(s)", out)
        self.assertEqual(MemberScheduleSeed.objects.count(), schedule_defaults.SEED_BATCH)
        self.assertIn("seeded 5 member(s)", self.tick())
        self.assertEqual(MemberScheduleSeed.objects.count(), schedule_defaults.SEED_BATCH + 5)
        self.assertIn("seeded 0 member(s)", self.tick())

    def test_the_time_budget_is_respected(self):
        self.member()
        summary = schedule_defaults.seed_pending(deadline=time.monotonic() - 1)
        self.assertEqual((summary.members, summary.schedules), (0, 0))
        self.assertFalse(MemberScheduleSeed.objects.exists())

    def test_one_members_failure_is_a_warning_and_the_rest_are_still_seeded(self):
        broken = self.member()
        others = [self.member(), self.member()]
        real = schedule_defaults._seed

        def flaky(user, team_id, now):
            if user.pk == broken.pk:
                raise RuntimeError("secret text from a task body")
            return real(user, team_id, now)

        with mock.patch.object(schedule_defaults, "_seed", side_effect=flaky):
            with self.assertLogs("origin.schedules", level="WARNING") as logs:
                out = self.tick()  # a CommandError here would fail the test: no ERROR logged
        self.assertIn("seeded 2 member(s)", out)
        self.assertIn("1 seeding failed", out)
        text = "\n".join(logs.output)
        self.assertIn("schedule_seed_failed", text)
        self.assertNotIn("secret text", text)
        self.assertNotIn("ERROR", text)
        for user in others:
            self.assertEqual(MemberSchedule.objects.filter(user=user).count(), 2)
        self.assertFalse(MemberScheduleSeed.objects.filter(user=broken).exists())

    def test_a_failure_halfway_leaves_no_marker_and_no_half_seed(self):
        user = self.member()
        real = schedules.create
        calls = []

        def second_fails(ctx, **kw):
            calls.append(kw["name"])
            if len(calls) == 2:
                raise RuntimeError("db")
            return real(ctx, **kw)

        with mock.patch.object(schedules, "create", side_effect=second_fails):
            summary = schedule_defaults.seed_pending()
        self.assertEqual(summary.failed, 1)
        self.assertFalse(MemberSchedule.objects.filter(user=user).exists())
        self.assertFalse(MemberScheduleSeed.objects.filter(user=user).exists())

    def test_the_dry_run_writes_nothing(self):
        self.member()
        out = StringIO()
        call_command("schedule_tick", "--dry-run", stdout=out)
        self.assertIn("1 member(s) would be seeded", out.getvalue())
        self.assertFalse(MemberScheduleSeed.objects.exists())

    def test_seeded_schedules_run_in_the_next_tick_as_ordinary_ones(self):
        # Seeded at 10:00 JST; the evening recap is due at 17:00 JST. A tick at 17:01 runs it.
        self.sign_in(self.user, 1)
        schedule_defaults.seed_pending(now=NOW)
        evening = MemberSchedule.objects.get(user=self.user, job="what_you_did")
        self.assertEqual(evening.next_run_at, datetime(2026, 11, 7, 8, 0, tzinfo=UTC))
        summary = schedules.run_due(datetime(2026, 11, 7, 8, 1, tzinfo=UTC))
        self.assertEqual((summary.due, summary.ran, summary.failed), (1, 1, 0))


class TestAccountOwnership(SeedBase):
    def test_deleting_the_account_removes_the_marker_and_only_theirs(self):
        schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        schedule_defaults.seed_defaults(self.user2, self.team, now=NOW)
        delete_account(self.user2)
        self.assertFalse(MemberScheduleSeed.objects.filter(user=self.user2).exists())
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())

    def test_the_export_says_when_and_where_they_were_seeded(self):
        schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        doc = build_export(self.user)
        self.assertEqual([d["team_id"] for d in doc["schedule_defaults_seeded"]], [self.team_id])
        self.assertTrue(doc["schedule_defaults_seeded"][0]["seeded_at"])
        self.assertEqual(len(doc["schedules"]), 2)

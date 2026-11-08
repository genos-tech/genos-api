"""Scheduled reports are Core and up (MIP-1213).

What is pinned here, each by a test that fails when the guard is removed:

  * the table: Free has no scheduled reports, Core, Pro, Max and Enterprise do; the plans payload
    carries the key; the reader fails OPEN (a missing or malformed value allows), because it
    gates a paid feature;
  * a Free member's attempt to create, edit, turn on or "run now" gets the upgrade sentence
    (REST: 403 with `code: plan_required`; MCP: the tool's error); listing, pausing and deleting
    still work, and the REST list says `planAllowed`;
  * no report is ever filed for a Free member (the tick), and a Core member's schedules run;
  * a downgrade pauses the schedules with the reason `plan` and keeps them; an upgrade resumes
    exactly those: a manual pause, a pause for failures or for leaving the team stays paused;
  * the suggested schedules are made for Core and up only, with no marker for a member who
    lacks the plan, so they arrive once when the member upgrades; Free members never use up
    the seeding pass.

The suite's own settings turn the capability on for every tier (`apis/settings_test.py`), so the
tests here put the PRODUCTION table back.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import (
    PAUSE_FAILURES,
    PAUSE_MANUAL,
    PAUSE_PLAN,
    MemberSchedule,
    MemberScheduleSeed,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import quota
from origin.services import schedule_defaults, schedules
from origin.services.schedule_jobs import JOBS, Job
from origin.tests.test_schedules import NOW, SCHEDULED, ScheduleBase, utc

TIERS = ("free", "core", "pro", "max", "enterprise")


def production(**per_tier):
    """The real `TIER_QUOTAS`, with `schedules_enabled` forced where a test says so."""
    table = {t: {**cfg} for t, cfg in settings.PRODUCTION_TIER_QUOTAS.items()}
    for tier, value in per_tier.items():
        if value is None:
            table[tier].pop("schedules_enabled", None)
        else:
            table[tier]["schedules_enabled"] = value
    return {**settings.SEARCH_ENGINE, "TIER_QUOTAS": table}


@override_settings(SEARCH_ENGINE=production())
class GateBase(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.set_plan("free")

    def tearDown(self):
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        super().tearDown()

    def set_plan(self, tier, team=None):
        TeamMaster.objects.filter(pk=(team or self.team).pk).update(plan=tier)
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def made_as_core(self, **kw):
        """A schedule that exists (made while the member had Core), then the plan changes."""
        self.set_plan("core")
        sched = self.make(**kw)
        return sched

    def reports(self):
        return InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)


class TestTheTable(GateBase):
    def test_free_has_none_and_every_paid_tier_has_them(self):
        table = settings.PRODUCTION_TIER_QUOTAS
        self.assertEqual({t for t in table if t in TIERS}, set(TIERS))
        self.assertIs(table["free"]["schedules_enabled"], False)
        for tier in ("core", "pro", "max", "enterprise"):
            self.assertIs(table[tier]["schedules_enabled"], True, tier)

    def test_the_reader_follows_the_members_effective_tier(self):
        for tier, expected in (("free", False), ("core", True), ("pro", True), ("max", True)):
            self.set_plan(tier)
            self.assertIs(quota.get_schedules_enabled(str(self.user.id)), expected, tier)

    def test_the_best_of_the_members_teams_counts(self):
        other = TeamMaster.objects.create(
            team_name="Paid", team_email="p@example.com", owner=self.user, plan="core"
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertTrue(quota.get_schedules_enabled(str(self.user.id)))

    def test_a_missing_or_malformed_value_is_the_tiers_own_default_not_a_blanket_allow(self):
        # `TIER_QUOTAS_JSON` replaces the whole table, so an override older than the key has no
        # `schedules_enabled` on Free: that must still read "no", and on a paid tier "yes".
        for tier, expected in (("free", False), ("core", True), ("max", True)):
            self.set_plan(tier)
            for value in (None, "no", 0, [], {}):
                with override_settings(SEARCH_ENGINE=production(**{tier: value})):
                    quota.invalidate_effective_tier([str(self.user.id)])
                    self.assertIs(
                        quota.get_schedules_enabled(str(self.user.id)), expected, (tier, value)
                    )

    def test_it_fails_open_when_the_tier_cannot_be_read(self):
        with mock.patch.object(quota, "get_effective_tier", side_effect=RuntimeError("redis")):
            self.assertTrue(quota.get_schedules_enabled(str(self.user.id)))

    def test_the_plans_payload_carries_the_key_for_every_tier(self):
        res = self.client.get("/api/v2/billing/plans/")
        self.assertEqual(res.status_code, 200)
        by_tier = {t["tier"]: t for t in res.data["tiers"]}
        for tier in TIERS:
            self.assertIn("schedules_enabled", by_tier[tier]["limits"], tier)
        self.assertIs(by_tier["free"]["limits"]["schedules_enabled"], False)
        self.assertIs(by_tier["core"]["limits"]["schedules_enabled"], True)


class TestAFreeMembersAttempts(GateBase):
    def refuse(self, call):
        with self.assertRaises(schedules.SchedulePlanRequired) as caught:
            call()
        self.assertEqual(str(caught.exception), schedules.PLAN_SENTENCE)
        self.assertIn("Core", schedules.PLAN_SENTENCE)

    def test_create_is_refused_with_the_upgrade_sentence_and_nothing_is_made(self):
        self.refuse(lambda: self.make())
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_edit_turning_on_and_run_now_are_refused(self):
        sched = self.made_as_core()
        schedules.set_enabled(self.ctx, sched.pk, False)
        self.set_plan("free")
        self.refuse(lambda: schedules.update(self.ctx, sched.pk, name="Renamed"))
        self.refuse(lambda: schedules.set_enabled(self.ctx, sched.pk, True))
        self.refuse(lambda: schedules.run_now(self.ctx, sched.pk))
        sched.refresh_from_db()
        self.assertEqual((sched.name, sched.enabled), ("Daily", False))
        self.assertEqual(self.reports().count(), 0)

    def test_listing_pausing_and_deleting_still_work(self):
        sched = self.made_as_core()
        self.set_plan("free")
        self.assertEqual([s.pk for s in schedules.list_schedules(self.ctx)], [sched.pk])
        paused = schedules.set_enabled(self.ctx, sched.pk, False)
        self.assertEqual(paused.paused_reason, PAUSE_MANUAL)
        schedules.delete(self.ctx, sched.pk)
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_core_member_can_do_all_of_it(self):
        self.set_plan("core")
        sched = self.make()
        schedules.update(self.ctx, sched.pk, name="Renamed")
        schedules.set_enabled(self.ctx, sched.pk, False)
        schedules.set_enabled(self.ctx, sched.pk, True)
        self.assertEqual(schedules.run_now(self.ctx, sched.pk)["status"], "ok")

    def test_the_other_member_in_the_same_team_is_judged_on_their_own_plan(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="free")
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        CustomUser.objects.filter(pk=self.user2.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        self.make(ctx=self.ctx2)
        self.refuse(lambda: self.make())


class TestTheTick(GateBase):
    def test_no_report_is_filed_for_a_free_member_and_the_schedule_is_kept_paused_for_the_plan(
        self,
    ):
        sched = self.made_as_core(due_at=SCHEDULED)
        self.set_plan("free")
        summary = schedules.run_due(NOW)
        self.assertEqual((summary.ran, summary.skipped_plan), (0, 1))
        self.assertEqual(self.reports().count(), 0)
        self.assertEqual(self.calls, [])
        sched.refresh_from_db()
        self.assertEqual(
            (sched.enabled, sched.paused_reason, sched.next_run_at), (False, PAUSE_PLAN, None)
        )

    def test_a_core_members_schedule_runs(self):
        self.made_as_core(due_at=SCHEDULED)
        summary = schedules.run_due(NOW)
        self.assertEqual((summary.ran, summary.skipped_plan), (1, 0))
        self.assertEqual(self.reports().count(), 1)

    def test_it_is_the_members_own_plan_that_counts_in_one_pass(self):
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        self.set_plan("free")
        CustomUser.objects.filter(pk=self.user2.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        schedules.create(
            self.ctx2,
            name="Theirs",
            job="ok",
            frequency="daily",
            time_of_day="09:00",
            now=utc(2026, 11, 1),
        )
        mine = MemberSchedule.objects.create(
            user_id=self.user.id,
            team_id=self.team.team_id,
            name="Mine",
            job="ok",
            params={},
            frequency="daily",
            time_of_day="09:00",
            tz_used="Asia/Tokyo",
            next_run_at=SCHEDULED,
        )
        MemberSchedule.objects.filter(user_id=self.user2.id).update(next_run_at=SCHEDULED)
        summary = schedules.run_due(NOW)
        self.assertEqual((summary.ran, summary.skipped_plan), (1, 1))
        self.assertEqual([r.receiver_id for r in self.reports()], [self.user2.id])
        mine.refresh_from_db()
        self.assertEqual(mine.paused_reason, PAUSE_PLAN)

    def test_the_tick_command_pauses_before_it_runs_and_says_so(self):
        sched = self.made_as_core(due_at=SCHEDULED)
        self.set_plan("free")
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            call_command("schedule_tick", stdout=out)
        self.assertIn("plan: 1 paused, 0 resumed", out.getvalue())
        self.assertEqual(self.reports().count(), 0)
        sched.refresh_from_db()
        self.assertEqual(sched.paused_reason, PAUSE_PLAN)


class TestFollowingThePlan(GateBase):
    def test_a_downgrade_pauses_every_running_schedule_and_keeps_them(self):
        a = self.made_as_core(name="A")
        b = self.make(name="B", time_of_day="10:00")
        self.set_plan("free")
        out = schedules.sync_plan(NOW)
        self.assertEqual((out.paused, out.resumed), (2, 0))
        for s in (a, b):
            s.refresh_from_db()
            self.assertEqual((s.enabled, s.paused_reason, s.next_run_at), (False, PAUSE_PLAN, None))
        self.assertEqual(MemberSchedule.objects.count(), 2)

    def test_an_upgrade_resumes_the_ones_paused_for_the_plan_from_now(self):
        sched = self.made_as_core(due_at=utc(2026, 1, 1))
        MemberSchedule.objects.filter(pk=sched.pk).update(consecutive_failures=2)
        self.set_plan("free")
        schedules.sync_plan(NOW)
        self.set_plan("pro")
        out = schedules.sync_plan(NOW)
        self.assertEqual((out.paused, out.resumed), (0, 1))
        sched.refresh_from_db()
        self.assertEqual(
            (sched.enabled, sched.paused_reason, sched.consecutive_failures), (True, "", 0)
        )
        self.assertGreater(sched.next_run_at, NOW)
        self.assertEqual(sched.tz_used, "Asia/Tokyo")

    def test_a_manual_pause_a_failure_pause_and_a_left_team_pause_stay_as_they_are(self):
        manual = self.made_as_core(name="Manual", time_of_day="08:00")
        failing = self.make(name="Failing", time_of_day="08:10")
        left = self.make(name="Left", time_of_day="08:20")
        schedules.set_enabled(self.ctx, manual.pk, False)
        MemberSchedule.objects.filter(pk=failing.pk).update(
            enabled=False, paused_reason=PAUSE_FAILURES, next_run_at=None
        )
        MemberSchedule.objects.filter(pk=left.pk).update(
            enabled=False, paused_reason="left_team", next_run_at=None
        )
        self.set_plan("free")
        self.assertEqual(schedules.sync_plan(NOW).paused, 0, "already paused for another reason")
        self.set_plan("core")
        self.assertEqual(schedules.sync_plan(NOW).resumed, 0)
        reasons = {s.name: s.paused_reason for s in MemberSchedule.objects.all()}
        self.assertEqual(
            reasons, {"Manual": PAUSE_MANUAL, "Failing": PAUSE_FAILURES, "Left": "left_team"}
        )
        self.assertFalse(MemberSchedule.objects.filter(enabled=True).exists())

    def test_a_plan_paused_schedule_of_a_member_who_left_the_team_is_not_resumed(self):
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        sched = self.made_as_core()
        self.set_plan("free")
        schedules.sync_plan(NOW)
        TeamMembers.objects.filter(attendee=self.user).update(is_deleted=True)
        # The member's OWN tier is Core, so the plan is not what keeps this one paused.
        CustomUser.objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertTrue(quota.get_schedules_enabled(str(self.user.id)))
        self.assertEqual(schedules.sync_plan(NOW).resumed, 0)
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)

    def test_a_one_off_whose_day_has_passed_is_not_resumed(self):
        sched = self.made_as_core(frequency="once", run_date="2030-01-01")
        self.set_plan("free")
        schedules.sync_plan(NOW)
        self.set_plan("core")
        self.assertEqual(schedules.sync_plan(utc(2031, 1, 1)).resumed, 0)
        sched.refresh_from_db()
        self.assertEqual(sched.paused_reason, PAUSE_PLAN)

    def test_it_is_idempotent_and_leaves_other_members_alone(self):
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        mine = self.made_as_core(name="Mine")
        CustomUser.objects.filter(pk=self.user2.pk).update(tier="pro")
        theirs = self.make(name="Theirs", ctx=self.ctx2, time_of_day="11:00")
        self.set_plan("free")
        self.assertEqual(schedules.sync_plan(NOW).paused, 1)
        self.assertEqual(schedules.sync_plan(NOW).paused, 0)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertFalse(mine.enabled)
        self.assertTrue(theirs.enabled)

    def test_a_pass_reads_in_pages_so_no_row_is_left_out_and_is_bounded_by_pages(self):
        for n in range(3):
            self.made_as_core(name=f"S{n}") if n == 0 else self.make(name=f"S{n}")
        self.set_plan("free")
        # One row per page: every row is still reached, none twice.
        self.assertEqual(schedules.sync_plan(NOW, limit=1).paused, 3)
        self.assertEqual(schedules.sync_plan(NOW, limit=1).paused, 0)

    def test_the_number_of_pages_bounds_one_pass(self):
        for n in range(3):
            self.made_as_core(name=f"S{n}") if n == 0 else self.make(name=f"S{n}")
        self.set_plan("free")
        self.assertEqual(schedules.sync_plan(NOW, limit=1, max_pages=2).paused, 2)
        self.assertEqual(schedules.sync_plan(NOW, limit=1, max_pages=2).paused, 1)

    def test_rows_of_a_member_who_upgraded_are_resumed_past_the_first_page(self):
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        mine = self.made_as_core(name="Mine")
        theirs = [self.make(name=f"T{n}", ctx=self.ctx2, time_of_day=f"1{n}:00") for n in range(3)]
        self.set_plan("free")
        schedules.sync_plan(NOW, limit=1)
        # Only the member with a Core tier of their own is resumed; the others stay held.
        CustomUser.objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id)])
        out = schedules.sync_plan(NOW, limit=1)
        self.assertEqual(out.resumed, 1)
        mine.refresh_from_db()
        self.assertTrue(mine.enabled)
        for sched in theirs:
            sched.refresh_from_db()
            self.assertEqual(sched.paused_reason, PAUSE_PLAN)


class TestTheDefaults(GateBase):
    SUGGESTED = ("what_to_do_today", "what_you_did")

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(
            JOBS,
            {
                key: Job(
                    key,
                    key,
                    "d",
                    lambda ctx, params: {"title": "t", "summary": "s", "sections": []},
                    {"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
                )
                for key in self.SUGGESTED
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def seeded_names(self, user):
        return sorted(MemberSchedule.objects.filter(user=user).values_list("name", flat=True))

    def test_a_free_member_is_not_seeded_and_gets_no_marker(self):
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])
        self.assertEqual(MemberSchedule.objects.filter(user=self.user).count(), 0)
        self.assertFalse(MemberScheduleSeed.objects.filter(user=self.user).exists())

    def test_the_member_is_seeded_once_when_they_upgrade(self):
        schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.set_plan("core")
        made = schedule_defaults.seed_defaults(self.user, self.team, now=NOW)
        self.assertEqual(sorted(s.name for s in made), ["Evening recap", "Morning plan"])
        self.assertTrue(MemberScheduleSeed.objects.filter(user=self.user).exists())
        self.assertEqual(schedule_defaults.seed_defaults(self.user, self.team, now=NOW), [])

    def test_the_lazy_seed_on_the_first_list_follows_the_plan_too(self):
        self.authenticate()
        res = self.client.get(f"/api/v2/schedules/?team_id={self.team.team_id}")
        self.assertEqual((res.status_code, res.data["schedules"]), (200, []))
        self.assertFalse(MemberScheduleSeed.objects.filter(user=self.user).exists())
        self.set_plan("core")
        res = self.client.get(f"/api/v2/schedules/?team_id={self.team.team_id}")
        self.assertEqual(len(res.data["schedules"]), 2)

    def test_the_pass_skips_free_members_without_using_up_its_batch(self):
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        free = [self.user2]
        for n in range(3):
            user = CustomUser.objects.create_user(
                username=f"free{n}", email=f"free{n}@example.com", password="x"
            )
            TeamMembers.objects.create(team=self.team, attendee=user)
            free.append(user)
        self.set_plan("free")
        CustomUser.objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(u.id) for u in (self.user, *free)])
        second = CustomUser.objects.create_user(
            username="core2", email="core2@example.com", password="x", tier="core"
        )
        TeamMembers.objects.create(team=self.team, attendee=second)
        quota.invalidate_effective_tier([str(second.id)])
        pending = mock.Mock(return_value=[*free, self.user, second])
        with mock.patch.object(schedule_defaults, "pending_members", pending):
            out = schedule_defaults.seed_pending(now=NOW, limit=1)
        self.assertEqual(out.members, 1, "the batch is still `limit` members")
        self.assertEqual(self.seeded_names(second), [])
        self.assertEqual(pending.call_args.kwargs["limit"], 1 * schedule_defaults.SCAN_FACTOR)
        self.assertEqual(self.seeded_names(self.user), ["Evening recap", "Morning plan"])
        self.assertEqual(sum(len(self.seeded_names(u)) for u in free), 0)


class TestOverRest(GateBase):
    URL = "/api/v2/schedules/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.team_id = str(self.team.team_id)

    def body(self, **over):
        return {
            "team_id": self.team_id,
            "name": "Morning",
            "job": "ok",
            "frequency": "daily",
            "timeOfDay": "09:00",
            **over,
        }

    def test_a_free_member_gets_403_plan_required_with_the_sentence(self):
        res = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data, {"error": schedules.PLAN_SENTENCE, "code": "plan_required"})
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_the_page_can_still_list_and_says_what_the_plan_allows(self):
        res = self.client.get(f"{self.URL}?team_id={self.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIs(res.data["planAllowed"], False)
        self.assertEqual(res.data["planMessage"], schedules.PLAN_SENTENCE)
        self.assertEqual(res.data["suggestions"], [])
        self.set_plan("core")
        res = self.client.get(f"{self.URL}?team_id={self.team_id}")
        self.assertIs(res.data["planAllowed"], True)
        self.assertEqual(res.data["planMessage"], "")

    def test_edit_resume_and_run_are_refused_but_pause_and_delete_work(self):
        sched = self.made_as_core()
        self.set_plan("free")
        url = f"{self.URL}{sched.id}/?team_id={self.team_id}"
        refused = [
            self.client.patch(url, {"name": "x"}, format="json"),
            self.client.post(f"{self.URL}{sched.id}/run/?team_id={self.team_id}"),
        ]
        for res in refused:
            self.assertEqual((res.status_code, res.data["code"]), (403, "plan_required"))
        self.assertEqual(self.client.patch(url, {"enabled": False}, format="json").status_code, 200)
        res = self.client.patch(url, {"enabled": True}, format="json")
        self.assertEqual((res.status_code, res.data["code"]), (403, "plan_required"))
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_a_core_member_creates_as_before(self):
        self.set_plan("core")
        res = self.client.post(self.URL, self.body(), format="json")
        self.assertEqual(res.status_code, 201, res.data)


class TestOverMcp(GateBase):
    def setUp(self):
        super().setUp()
        from origin.models.common.api_key_models import ApiKey, generate_key, hash_key  # noqa: PLC0415

        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def call(self, name, args):
        res = self.client.post(
            f"/api/public/v1/mcp?team_id={self.team.team_id}",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
            format="json",
        )
        return res.json()["result"]

    def test_the_tool_gives_the_upgrade_sentence_as_its_error(self):
        # MCP itself is Core and up, so give Free that and keep the schedule gate real.
        table = production(free=False)
        table["TIER_QUOTAS"]["free"]["mcp_enabled"] = True
        with override_settings(SEARCH_ENGINE=table):
            quota.invalidate_effective_tier([str(self.user.id)])
            result = self.call(
                "create_my_schedule",
                {"name": "x", "job": "ok", "frequency": "daily", "time_of_day": "09:00"},
            )
        self.assertTrue(result["isError"])
        self.assertIn(schedules.PLAN_SENTENCE, json.dumps(result))
        self.assertEqual(MemberSchedule.objects.count(), 0)

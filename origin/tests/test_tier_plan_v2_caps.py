"""Tier plan v2 counts (MIP-1960): webhook endpoints per team, scheduled reports per person.

Decided 2026-11-22 (note "Decision: tier plan v2 (MCP-first) adopted with every recommendation"):
webhooks Free 0 / Core 3 / Pro 10 / Max 25 / Enterprise unlimited; schedules Core 5 / Pro 20 /
Max 20 / Enterprise 20 (Free none). A cap refuses only NEW rows; what exists is kept and works.

The suite's own settings switch both caps off (`apis/settings_test.py`), so these tests put the
PRODUCTION table back.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings

from origin.models.common.schedule_models import MemberSchedule
from origin.models.common.team_models import TeamMaster
from origin.models.common.webhook_models import EVENT_TASK_CREATED, WebhookEndpoint
from origin.search_engine import credit_ledger, quota
from origin.search_engine.models import AiCreditEntry
from origin.services import schedules
from origin.tests import test_report_ai_sentences as ai_tests
from origin.tests.spend_limit_support import over_the_monthly_budget
from origin.tests.test_schedules import ScheduleBase
from origin.tests.test_webhooks import WEBHOOKS, WebhookApiBase

WEBHOOK_CAPS = {"free": 0, "core": 3, "pro": 10, "max": 25, "enterprise": None}
SCHEDULE_CAPS = {"free": 0, "core": 5, "pro": 20, "max": 20, "enterprise": 20}


def production(drop=()):
    """The real `TIER_QUOTAS`, optionally with keys removed (an old `TIER_QUOTAS_JSON`)."""
    table = {t: {**cfg} for t, cfg in settings.PRODUCTION_TIER_QUOTAS.items()}
    for cfg in table.values():
        for key in drop:
            cfg.pop(key, None)
    return {**settings.SEARCH_ENGINE, "TIER_QUOTAS": table}


@override_settings(SEARCH_ENGINE=production())
class TableTests(WebhookApiBase):
    def test_the_table_is_the_decided_one(self):
        for tier, cap in WEBHOOK_CAPS.items():
            self.assertEqual(quota.get_webhooks_max(tier), cap, tier)

    def test_a_missing_key_reads_as_the_tiers_own_default(self):
        with override_settings(SEARCH_ENGINE=production(drop=("webhooks_max", "schedules_max"))):
            for tier, cap in WEBHOOK_CAPS.items():
                self.assertEqual(quota.get_webhooks_max(tier), cap, tier)

    def test_a_malformed_value_reads_as_the_tiers_default(self):
        table = production()
        table["TIER_QUOTAS"]["core"]["webhooks_max"] = "three"
        table["TIER_QUOTAS"]["free"]["webhooks_max"] = -4
        with override_settings(SEARCH_ENGINE=table):
            self.assertEqual(quota.get_webhooks_max("core"), 3)
            self.assertEqual(quota.get_webhooks_max("free"), 0)

    def test_an_unknown_tier_is_free(self):
        self.assertEqual(quota.get_webhooks_max("platinum"), 0)

    def test_schedule_caps_follow_the_effective_tier(self):
        for tier, cap in SCHEDULE_CAPS.items():
            TeamMaster.objects.filter(pk=self.team.pk).update(plan=tier)
            quota.invalidate_effective_tier([str(self.user.id)])
            self.assertEqual(quota.get_schedules_max(str(self.user.id)), cap, tier)

    def test_a_tier_lookup_error_is_unlimited_here_and_the_old_ceiling_in_the_service(self):
        with patch.object(quota, "get_effective_tier", side_effect=RuntimeError("redis")):
            self.assertIsNone(quota.get_schedules_max(str(self.user.id)))
            self.assertEqual(
                schedules.schedule_limit(str(self.user.id)), schedules.MAX_SCHEDULES_PER_MEMBER
            )


@override_settings(SEARCH_ENGINE=production())
class WebhookCapTests(WebhookApiBase):
    def set_plan(self, tier):
        TeamMaster.objects.filter(pk=self.team.pk).update(plan=tier)
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def create(self):
        self.authenticate(self.user)
        with self._public_dns():
            return self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )

    def fill(self, n):
        for _ in range(n):
            self._endpoint()

    def test_free_cannot_create_one(self):
        self.set_plan("free")
        res = self.create()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "plan_limit")
        self.assertEqual(res.data["category"], "webhooks")
        self.assertEqual((res.data["used"], res.data["limit"]), (0, 0))
        self.assertIn("not included in the Free plan", res.data["error"])
        self.assertEqual(WebhookEndpoint.objects.filter(team=self.team).count(), 0)

    def test_each_tier_refuses_the_one_over_its_cap_and_allows_the_last_under_it(self):
        for tier, cap in WEBHOOK_CAPS.items():
            if cap is None or cap == 0:
                continue
            with self.subTest(tier=tier):
                WebhookEndpoint.objects.filter(team=self.team).delete()
                self.set_plan(tier)
                self.fill(cap - 1)
                self.assertEqual(self.create().status_code, 201)
                refused = self.create()
                self.assertEqual(refused.status_code, 429)
                self.assertEqual((refused.data["used"], refused.data["limit"]), (cap, cap))
                self.assertEqual(WebhookEndpoint.objects.filter(team=self.team).count(), cap)

    def test_enterprise_is_unlimited(self):
        self.set_plan("enterprise")
        self.fill(30)
        self.assertEqual(self.create().status_code, 201)

    def test_a_free_team_whose_owner_pays_personally_may_create_one(self):
        # The same rule as everywhere else: the better of the team's plan and the owner's
        # effective personal tier (MIP-1969).
        self.set_plan("free")
        type(self.user).objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertEqual(self.create().status_code, 201)

    def test_a_free_team_whose_owner_is_in_the_first_month_trial_may_create_one(self):
        from datetime import timedelta

        from django.utils import timezone

        from origin.models.common.trial_models import CoreTrial

        self.set_plan("free")
        CoreTrial.objects.create(user=self.user, ends_at=timezone.now() + timedelta(days=10))
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertEqual(self.create().status_code, 201)

    def test_a_paid_team_with_a_free_owner_keeps_its_own_cap(self):
        self.set_plan("core")
        self.assertEqual(self.create().status_code, 201)

    def test_the_cap_is_the_better_tier_not_the_sum(self):
        self.set_plan("free")
        type(self.user).objects.filter(pk=self.user.pk).update(tier="core")
        quota.invalidate_effective_tier([str(self.user.id)])
        self.fill(2)
        self.assertEqual(self.create().status_code, 201)
        refused = self.create()
        self.assertEqual(refused.status_code, 429)
        self.assertEqual((refused.data["used"], refused.data["limit"]), (3, 3))

    def test_a_non_owner_creators_own_tier_does_not_count(self):
        from origin.models.common.team_models import TeamMembers

        self.set_plan("free")
        TeamMembers.objects.update_or_create(
            team=self.team, attendee=self.user2, defaults={"member_role": "editor"}
        )
        type(self.user2).objects.filter(pk=self.user2.pk).update(tier="max")
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])
        self.authenticate(self.user2)
        with self._public_dns():
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 429)

    def test_a_downgraded_team_keeps_its_endpoints_and_a_delete_frees_a_slot(self):
        self.set_plan("pro")
        self.fill(8)
        self.set_plan("core")  # cap 3, has 8
        self.authenticate(self.user)
        listed = self.client.get(WEBHOOKS, {"team_id": str(self.team.team_id)})
        self.assertEqual(len(listed.data["webhooks"]), 8)
        self.assertTrue(all(w["is_active"] for w in listed.data["webhooks"]))
        self.assertEqual(self.create().status_code, 429)
        for w in WebhookEndpoint.objects.filter(team=self.team)[:6]:
            self.client.delete(f"{WEBHOOKS}{w.id}/")
        self.assertEqual(WebhookEndpoint.objects.filter(team=self.team).count(), 2)
        self.assertEqual(self.create().status_code, 201)


@override_settings(SEARCH_ENGINE=production())
class ScheduleCapTests(ScheduleBase):
    def set_plan(self, tier):
        TeamMaster.objects.filter(pk=self.team.pk).update(plan=tier)
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def test_each_paid_tier_refuses_the_schedule_over_its_cap(self):
        for tier, cap in SCHEDULE_CAPS.items():
            if cap == 0:
                continue
            with self.subTest(tier=tier):
                MemberSchedule.objects.filter(user_id=self.user.pk).delete()
                self.set_plan(tier)
                for n in range(cap):
                    self.make(name=f"s{n}")
                with self.assertRaises(schedules.ScheduleLimitReached) as caught:
                    self.make(name="one more")
                self.assertEqual((caught.exception.used, caught.exception.limit), (cap, cap))
                self.assertIn(f"allows {cap} schedules", str(caught.exception))
                self.assertEqual(schedules.schedule_limit(self.user.pk), cap)

    def test_free_has_none_the_plan_sentence_comes_first(self):
        self.set_plan("free")
        with self.assertRaises(schedules.SchedulePlanRequired):
            self.make()

    def test_a_downgraded_member_keeps_and_runs_what_exists_but_cannot_add(self):
        self.set_plan("pro")
        made = [self.make(name=f"s{n}") for n in range(8)]
        self.set_plan("core")  # cap 5, has 8
        self.assertEqual(len(schedules.list_schedules(self.ctx)), 8)
        with self.assertRaises(schedules.ScheduleLimitReached):
            self.make(name="new")
        # Pausing, resuming and deleting are never blocked by the count.
        schedules.set_enabled(self.ctx, str(made[0].id), False)
        schedules.set_enabled(self.ctx, str(made[0].id), True)
        schedules.delete(self.ctx, str(made[1].id))
        self.assertEqual(MemberSchedule.objects.filter(user_id=self.user.pk).count(), 7)

    def test_the_rest_answer_carries_the_numbers_and_the_list_the_plans_limit(self):
        self.set_plan("core")
        self.authenticate(self.user)
        url = "/api/v2/schedules/"
        for n in range(5):
            self.make(name=f"s{n}")
        listed = self.client.get(url, {"team_id": str(self.team.team_id)})
        self.assertEqual(listed.data["limit"], 5)
        res = self.client.post(
            url,
            {
                "team_id": str(self.team.team_id),
                "name": "one more",
                "job": "ok",
                "frequency": "daily",
                "timeOfDay": "12:00",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "plan_limit")
        self.assertEqual(res.data["category"], "schedules")
        self.assertEqual((res.data["used"], res.data["limit"]), (5, 5))


CREDITS_SHADOW = {"AI_CREDITS_SHADOW": True, "AI_CREDITS_AUTHORITATIVE": False}
CREDITS_AUTHORITATIVE = {"AI_CREDITS_SHADOW": True, "AI_CREDITS_AUTHORITATIVE": True}


def with_credits(flags):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ai_tests.LIVE, **flags})


class ScheduleCreditTests(ai_tests.AiBase):
    """A report with AI sentences is charged to AI credits (MIP-1960), once per run that called
    the model; out of credits (credits authoritative) the report runs without them and says so."""

    def run_ai(self, task_comment="Waiting for the vendor."):
        self.overdue(comment=task_comment)
        self.model.reply = ai_tests.answer(t1="The vendor has not answered.")
        self.model.spend_tokens = 200_000
        return self.run_job("what_to_do_today")

    def charges(self):
        return AiCreditEntry.objects.filter(
            user_id=str(self.user.id), entry_type=AiCreditEntry.ENTRY_CHARGE
        )

    def test_schedule_is_a_billable_surface(self):
        self.assertIn("schedule", settings.CREDIT_POLICY.billable_surfaces)
        self.assertNotIn("agent_digest", settings.CREDIT_POLICY.billable_surfaces)

    def test_a_run_that_called_the_model_posts_exactly_one_charge(self):
        with with_credits(CREDITS_SHADOW):
            report = self.run_ai()
            self.assertEqual(len(self.model.calls), 1)
            rows = list(self.charges())
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0].request_id), report["ai_request_id"])
            self.assertLess(rows[0].credits_milli, 0)

    def test_settling_twice_leaves_one_row(self):
        from origin.search_engine import spend_recorder  # noqa: PLC0415
        from origin.search_engine.llm import spend  # noqa: PLC0415

        with with_credits(CREDITS_SHADOW):
            report = self.run_ai()
            ctx = spend.SpendContext(
                request_id=report["ai_request_id"], user_id=str(self.user.id), surface="schedule"
            )
            spend_recorder.settle_shadow_charge(ctx)
            self.assertEqual(self.charges().count(), 1)

    def test_a_fully_cached_run_makes_no_call_and_posts_nothing_more(self):
        with with_credits(CREDITS_SHADOW):
            self.run_ai()
            self.run_job("what_to_do_today")
            self.assertEqual(len(self.model.calls), 1)
            self.assertEqual(self.charges().count(), 1)

    def test_out_of_credits_the_report_runs_without_ai_and_says_so(self):
        with (
            with_credits(CREDITS_AUTHORITATIVE),
            mock.patch.object(credit_ledger, "balance_milli", return_value=0),
        ):
            report = self.run_ai()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(report["ai_skipped"], "no_credits")
        self.assertIn("no AI credits left", report["summary"])
        self.assertEqual(self.charges().count(), 0)

    def test_a_reached_monthly_limit_runs_without_ai_and_says_so(self):
        with with_credits(CREDITS_SHADOW), over_the_monthly_budget():
            report = self.run_ai()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(report["ai_skipped"], "monthly_limit")
        self.assertIn("monthly limit", report["summary"])
        self.assertNotIn("no AI credits left", report["summary"])
        self.assertEqual(self.charges().count(), 0)

    def test_the_inbox_card_carries_a_monthly_limit(self):
        from origin.services import inbox_ai  # noqa: PLC0415

        item = mock.Mock(
            item_id=1,
            item_type=inbox_ai.ITEM_TYPE_REPORT,
            item_optionals={
                "schedule_id": "00000000-0000-0000-0000-000000000001",
                "ai_skipped": "monthly_limit",
            },
        )
        source = inbox_ai.sources_for(self.user, [item])[1]
        self.assertEqual(source.public()["ai_skipped"], "monthly_limit")

    def test_shadow_only_never_blocks(self):
        with (
            with_credits(CREDITS_SHADOW),
            mock.patch.object(credit_ledger, "balance_milli", return_value=0),
        ):
            report = self.run_ai()
        self.assertEqual(len(self.model.calls), 1)
        self.assertNotIn("no AI credits left", report["summary"])

    def test_an_unlimited_plan_is_never_out(self):
        with (
            with_credits(CREDITS_AUTHORITATIVE),
            mock.patch.object(credit_ledger, "balance_milli", return_value=None),
        ):
            self.run_ai()
        self.assertEqual(len(self.model.calls), 1)

    def test_the_inbox_card_is_told_why_there_was_no_ai_part(self):
        from origin.services import inbox_ai  # noqa: PLC0415

        item = mock.Mock(
            item_id=1,
            item_type=inbox_ai.ITEM_TYPE_REPORT,
            item_optionals={
                "schedule_id": "00000000-0000-0000-0000-000000000001",
                "ai_skipped": "no_credits",
            },
        )
        source = inbox_ai.sources_for(self.user, [item])[1]
        self.assertEqual(source.public()["ai_skipped"], "no_credits")
        self.assertFalse(source.public()["used_ai"])

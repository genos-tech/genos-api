"""Tests for monthly quota counting + effective-tier resolution.

Covers `origin.search_engine.quota`:
  - `get_used_month` / `check_remaining_monthly` (calendar-month sum,
    loop-aware `n`, None=unlimited, fail-open on infra errors).
  - `increment_usage(amount=)`.
  - `resolve_effective_tier` / `get_effective_tier` (personal vs team
    plan, rank ordering, membership/team soft-delete, fail-open).
  - retention / upload helpers reading the new TIER_QUOTAS keys.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.usage_models import ModelUsageCounter
from origin.search_engine import quota
from origin.tests.test_base import BaseAPITestCase


def _search_engine_with_quotas(tier_quotas):
    """settings.SEARCH_ENGINE with only TIER_QUOTAS replaced."""
    se = dict(settings.SEARCH_ENGINE)
    se["TIER_QUOTAS"] = tier_quotas
    return se


TEST_QUOTAS = {
    "free": {
        "llm_ask_daily": 20,
        "web_search_daily": 10,
        "model_daily": {},
        "task_create_monthly": 10,
        "note_create_monthly": 5,
        "message_retention_days": 90,
        "upload_max_mb": 10,
    },
    "pro": {
        "llm_ask_daily": 100,
        "web_search_daily": 50,
        "model_daily": {},
        "task_create_monthly": 100,
        "note_create_monthly": 50,
        "message_retention_days": None,
        "upload_max_mb": 25,
    },
    "max": {
        "llm_ask_daily": 1000,
        "web_search_daily": 500,
        "model_daily": {},
        "task_create_monthly": None,
        "note_create_monthly": None,
        "message_retention_days": None,
        "upload_max_mb": 100,
    },
    "enterprise": {
        "llm_ask_daily": None,
        "web_search_daily": None,
        "model_daily": {},
        "task_create_monthly": None,
        "note_create_monthly": None,
        "message_retention_days": None,
        "upload_max_mb": None,
    },
}


class QuotaTestCase(BaseAPITestCase):
    """Fixtures + cache hygiene shared by the quota tests."""

    def setUp(self):
        super().setUp()
        self._evict_tiers()

    def tearDown(self):
        self._evict_tiers()
        super().tearDown()

    def _evict_tiers(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class MonthlyCountingTests(QuotaTestCase):
    def test_get_used_month_sums_daily_rows(self):
        today = timezone.now().date()
        month_start = today.replace(day=1)
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=month_start,
            count=3,
        )
        expected = 3
        if today != month_start:
            ModelUsageCounter.objects.create(
                user=self.user,
                model_name=quota.TASK_CREATE_KEY,
                usage_date=today,
                count=4,
            )
            expected += 4
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), expected)

    def test_last_month_rows_excluded(self):
        month_start = timezone.now().date().replace(day=1)
        last_month = month_start - timedelta(days=1)
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=last_month,
            count=99,
        )
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 0)

    def test_other_users_rows_excluded(self):
        ModelUsageCounter.objects.create(
            user=self.user2,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=timezone.now().date(),
            count=7,
        )
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 0)

    def test_check_remaining_monthly_is_loop_aware(self):
        # free tier: task_create_monthly = 10; 8 already used.
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=timezone.now().date(),
            count=8,
        )
        allowed, used, limit = quota.check_remaining_monthly(
            self.user.id, quota.TASK_CREATE_KEY, n=2
        )
        self.assertTrue(allowed)
        self.assertEqual((used, limit), (8, 10))

        allowed, used, limit = quota.check_remaining_monthly(
            self.user.id, quota.TASK_CREATE_KEY, n=3
        )
        self.assertFalse(allowed)
        self.assertEqual((used, limit), (8, 10))

    def test_none_limit_means_unlimited(self):
        self.user.tier = "max"  # task_create_monthly: None
        self.user.save(update_fields=["tier"])
        self._evict_tiers()
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=timezone.now().date(),
            count=10_000,
        )
        allowed, used, limit = quota.check_remaining_monthly(
            self.user.id, quota.TASK_CREATE_KEY, n=500
        )
        self.assertTrue(allowed)
        self.assertEqual(used, 10_000)
        self.assertIsNone(limit)

    def test_fail_open_on_internal_error(self):
        with mock.patch.object(quota, "get_used_month", side_effect=Exception("db down")):
            allowed, used, limit = quota.check_remaining_monthly(
                self.user.id, quota.TASK_CREATE_KEY
            )
        self.assertTrue(allowed)
        self.assertEqual(used, 0)
        self.assertIsNone(limit)

    def test_increment_usage_amount(self):
        quota.increment_usage(self.user.id, quota.TASK_CREATE_KEY, amount=5)
        quota.increment_usage(self.user.id, quota.TASK_CREATE_KEY, amount=2)
        quota.increment_usage(self.user.id, quota.TASK_CREATE_KEY)  # default 1
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 8)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class EffectiveTierTests(QuotaTestCase):
    def test_personal_tier_only(self):
        resolved = quota.resolve_effective_tier(self.user.id)
        self.assertEqual(resolved["tier"], "free")
        self.assertEqual(resolved["source"], "personal")
        self.assertIsNone(resolved["team_name"])

    def test_team_plan_lifts_member(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        resolved = quota.resolve_effective_tier(self.user2.id)
        self.assertEqual(resolved["tier"], "pro")
        self.assertEqual(resolved["source"], "team")
        self.assertEqual(resolved["team_name"], self.team.team_name)

    def test_higher_personal_tier_wins_over_team(self):
        self.user.tier = "max"
        self.user.save(update_fields=["tier"])
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        resolved = quota.resolve_effective_tier(self.user.id)
        self.assertEqual(resolved["tier"], "max")
        self.assertEqual(resolved["source"], "personal")

    def test_enterprise_team_ranks_highest(self):
        self.user.tier = "max"
        self.user.save(update_fields=["tier"])
        self.team.plan = "enterprise"
        self.team.save(update_fields=["plan"])
        self.assertEqual(quota.get_effective_tier(self.user.id), "enterprise")

    def test_leaving_team_reverts_to_personal(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        self.assertEqual(quota.get_effective_tier(self.user2.id), "pro")

        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self._evict_tiers()
        self.assertEqual(quota.get_effective_tier(self.user2.id), "free")

    def test_deleted_team_plan_ignored(self):
        self.team.plan = "pro"
        self.team.is_deleted = True
        self.team.save(update_fields=["plan", "is_deleted"])
        self.assertEqual(quota.get_effective_tier(self.user.id), "free")

    def test_best_plan_among_multiple_teams_wins(self):
        other = TeamMaster.objects.create(
            team_name="Second Team",
            team_email="team2@example.com",
            owner=self.user,
            plan="max",
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        resolved = quota.resolve_effective_tier(self.user.id)
        self.assertEqual(resolved["tier"], "max")
        self.assertEqual(resolved["team_name"], "Second Team")

    def test_fail_open_to_personal_on_team_lookup_error(self):
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        with mock.patch(
            "origin.search_engine.quota.TeamMaster.objects.filter",
            side_effect=Exception("db down"),
        ):
            resolved = quota.resolve_effective_tier(self.user.id)
        self.assertEqual(resolved["tier"], "pro")
        self.assertEqual(resolved["source"], "personal")

    def test_quota_resolution_uses_effective_tier(self):
        # free personal + pro team → the PRO monthly cap applies.
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        self.assertEqual(quota.get_quota(self.user.id, quota.TASK_CREATE_KEY), 100)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class RetentionAndUploadHelperTests(QuotaTestCase):
    def test_free_retention_and_upload(self):
        self.assertEqual(quota.get_message_retention_days(self.user.id), 90)
        self.assertEqual(quota.get_upload_max_bytes(self.user.id), 10 * 1024 * 1024)

    def test_paid_retention_unlimited(self):
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        self._evict_tiers()
        self.assertIsNone(quota.get_message_retention_days(self.user.id))
        self.assertEqual(quota.get_upload_max_bytes(self.user.id), 25 * 1024 * 1024)

    def test_enterprise_upload_unlimited(self):
        self.user.tier = "enterprise"
        self.user.save(update_fields=["tier"])
        self._evict_tiers()
        self.assertIsNone(quota.get_upload_max_bytes(self.user.id))


class DefaultConfigShapeTests(TestCase):
    """The shipped TIER_QUOTAS must carry every dimension for every tier."""

    REQUIRED_KEYS = {
        "llm_ask_daily",
        "web_search_daily",
        "model_daily",
        "task_create_monthly",
        "note_create_monthly",
        "message_retention_days",
        "upload_max_mb",
    }

    def test_all_tiers_have_all_dimensions(self):
        tier_quotas = settings.SEARCH_ENGINE["TIER_QUOTAS"]
        self.assertEqual(set(tier_quotas.keys()), {"free", "pro", "max", "enterprise"})
        for tier, cfg in tier_quotas.items():
            self.assertTrue(
                self.REQUIRED_KEYS.issubset(cfg.keys()),
                f"tier '{tier}' missing keys: {self.REQUIRED_KEYS - set(cfg.keys())}",
            )

    def test_new_dimensions_ship_dark(self):
        # The four new dimensions must stay None (unlimited) until the
        # deliberate enable PR — flipping them is a product decision,
        # not a side effect.
        for tier, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items():
            for key in (
                "task_create_monthly",
                "note_create_monthly",
                "message_retention_days",
                "upload_max_mb",
            ):
                self.assertIsNone(cfg[key], f"{tier}.{key} flipped early")

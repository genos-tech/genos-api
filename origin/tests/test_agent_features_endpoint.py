"""Tests for the extended GET /api/v2/agent/features/ payload.

The endpoint is the single fetch behind the Settings "Plan & Usage"
tab: effective tier (+ source), the two daily AI quota blocks, the
two monthly creation blocks, and the retention / upload dimensions.
All additions are additive — old clients keep reading tier / llm_ask /
web_search unchanged.
"""

from django.test import override_settings
from django.utils import timezone

from origin.models.common.usage_models import ModelUsageCounter
from origin.search_engine import quota

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

URL = "/api/v2/agent/features/"


class FeaturesTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class AgentFeaturesPayloadTests(FeaturesTestBase):
    def test_free_user_full_payload(self):
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=quota.TASK_CREATE_KEY,
            usage_date=timezone.now().date(),
            count=3,
        )
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        data = res.data
        self.assertEqual(data["tier"], "free")
        self.assertEqual(data["tier_source"], "personal")
        self.assertIsNone(data["tier_team"])
        # Pre-existing keys unchanged in shape.
        self.assertEqual(set(data["llm_ask"].keys()), {"used", "limit"})
        self.assertEqual(data["llm_ask"]["limit"], 20)
        self.assertEqual(data["web_search"]["limit"], 10)
        # New monthly blocks carry the period label + real usage.
        self.assertEqual(data["task_create"], {"used": 3, "limit": 10, "period": "month"})
        self.assertEqual(data["note_create"], {"used": 0, "limit": 5, "period": "month"})
        self.assertEqual(data["message_retention_days"], 90)
        self.assertEqual(data["upload_max_mb"], 10)

    def test_team_plan_reflected_with_source_and_name(self):
        self.team.plan = "max"
        self.team.save(update_fields=["plan"])
        quota.invalidate_effective_tier([self.user.id])
        res = self.client.get(URL)
        data = res.data
        self.assertEqual(data["tier"], "max")
        self.assertEqual(data["tier_source"], "team")
        self.assertEqual(data["tier_team"], self.team.team_name)
        self.assertIsNone(data["message_retention_days"])
        self.assertIsNone(data["task_create"]["limit"])
        self.assertEqual(data["upload_max_mb"], 100)


class AgentFeaturesDarkDefaultsTests(FeaturesTestBase):
    """SHIPPED config: the new dimensions are all None (unlimited)."""

    def test_new_dimensions_null_by_default(self):
        res = self.client.get(URL)
        data = res.data
        self.assertIsNone(data["task_create"]["limit"])
        self.assertIsNone(data["note_create"]["limit"])
        self.assertIsNone(data["message_retention_days"])
        self.assertIsNone(data["upload_max_mb"])
        # Daily AI quotas keep their existing live values.
        self.assertIsNotNone(data["llm_ask"]["limit"])

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

    def test_capability_keys_present_and_permissive_when_unset(self):
        # TEST_QUOTAS predates the UX-pillar keys, so this pins BOTH
        # that the payload always carries them AND that a config table
        # without them resolves permissive (the TIER_QUOTAS_JSON
        # override contract) — except digest_cadence, which stays off.
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        data = res.data
        self.assertEqual(data["agent_tool_level"], "organize")
        self.assertEqual(data["max_effort"], "high")
        self.assertTrue(data["auto_effort"])
        self.assertEqual(data["agent_memory"], "team")
        self.assertIsNone(data["agent_history_retention_days"])
        self.assertEqual(data["integrations"], ["web", "google_calendar", "github"])
        self.assertIsNone(data["digest_cadence"])

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


class AgentFeaturesShippedDefaultsTests(FeaturesTestBase):
    """SHIPPED config: the free tier's limits are live.

    Five modules assert the shipped tier table:
      test_quota_monthly, test_creation_caps, test_message_retention,
      test_upload_size_tiers, test_agent_features_endpoint.
    Changing a cap means changing SUBSCRIPTION_TIERS.md and all five.
    """

    def test_shipped_free_limits_populated(self):
        res = self.client.get(URL)
        data = res.data
        self.assertEqual(data["task_create"]["limit"], 50)
        self.assertEqual(data["note_create"]["limit"], 50)
        self.assertEqual(data["message_retention_days"], 180)
        self.assertEqual(data["upload_max_mb"], 5)
        # Daily AI quotas keep their existing live values.
        self.assertIsNotNone(data["llm_ask"]["limit"])

    def test_shipped_free_capability_ladder(self):
        # The flipped experience ladder (UX_TIER_MODEL_PLAN.md §3.1):
        # Free's Genos answers, at the default depth, remembering only
        # the live conversation, seeing only the workspace.
        res = self.client.get(URL)
        data = res.data
        self.assertEqual(data["agent_tool_level"], "read")
        self.assertEqual(data["max_effort"], "low")
        self.assertFalse(data["auto_effort"])
        self.assertEqual(data["agent_memory"], "none")
        self.assertEqual(data["agent_history_retention_days"], 30)
        self.assertEqual(data["integrations"], [])
        self.assertIsNone(data["digest_cadence"])

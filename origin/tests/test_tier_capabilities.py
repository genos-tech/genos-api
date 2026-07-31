"""Tests for the UX-pillar capability accessors in `origin.search_engine.quota`.

The tier model's experience ladder (genos-docs
operations/UX_TIER_MODEL_PLAN.md) resolves through seven TIER_QUOTAS
keys. Two contracts pinned here:

  - Fail-open: an infra error, a MISSING key (a TIER_QUOTAS_JSON
    override written before the key existed), or an unknown value all
    resolve PERMISSIVE — never a downgrade. `digest_cadence` is the
    deliberate exception: on any doubt, no unsolicited digest.
  - Explicit restrictive values pass through untouched — including
    `integrations: []`, which is a real restriction, not a missing key.
"""

from unittest import mock

from django.test import override_settings

from origin.search_engine import quota

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

# TEST_QUOTAS predates the capability keys on purpose: running against
# it IS the missing-key test.
_LEGACY_QUOTAS = TEST_QUOTAS

_RESTRICTED_QUOTAS = {
    **TEST_QUOTAS,
    "free": {
        **TEST_QUOTAS["free"],
        "agent_tool_level": "read",
        "max_effort": "low",
        "auto_effort": False,
        "agent_memory": "none",
        "agent_history_retention_days": 30,
        "integrations": [],
        "digest_cadence": "weekly",
    },
}

_GARBAGE_QUOTAS = {
    **TEST_QUOTAS,
    "free": {
        **TEST_QUOTAS["free"],
        "agent_tool_level": "superuser",
        "max_effort": "ultra",
        "agent_memory": "everything",
        "integrations": "github",
        "digest_cadence": "hourly",
    },
}


class CapabilityAccessorTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_LEGACY_QUOTAS))
    def test_missing_keys_resolve_permissive(self):
        uid = str(self.user.id)
        self.assertEqual(quota.get_agent_tool_level(uid), "organize")
        self.assertEqual(quota.get_max_effort(uid), "high")
        self.assertTrue(quota.get_auto_effort(uid))
        self.assertEqual(quota.get_agent_memory(uid), "team")
        self.assertIsNone(quota.get_agent_history_retention_days(uid))
        self.assertEqual(
            quota.get_integrations(uid), ["web", "google_calendar", "github"]
        )
        # The exception: a missing cadence means NO digest, not "daily".
        self.assertIsNone(quota.get_digest_cadence(uid))

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_RESTRICTED_QUOTAS))
    def test_explicit_values_pass_through(self):
        uid = str(self.user.id)
        self.assertEqual(quota.get_agent_tool_level(uid), "read")
        self.assertEqual(quota.get_max_effort(uid), "low")
        self.assertFalse(quota.get_auto_effort(uid))
        self.assertEqual(quota.get_agent_memory(uid), "none")
        self.assertEqual(quota.get_agent_history_retention_days(uid), 30)
        # [] is a restriction, not an accident — it must NOT widen.
        self.assertEqual(quota.get_integrations(uid), [])
        self.assertEqual(quota.get_digest_cadence(uid), "weekly")

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_GARBAGE_QUOTAS))
    def test_unknown_values_normalize_permissive(self):
        uid = str(self.user.id)
        self.assertEqual(quota.get_agent_tool_level(uid), "organize")
        self.assertEqual(quota.get_max_effort(uid), "high")
        self.assertEqual(quota.get_agent_memory(uid), "team")
        # A bare string (not a list) is malformed config, not a grant.
        self.assertEqual(
            quota.get_integrations(uid), ["web", "google_calendar", "github"]
        )
        # Unknown cadence: silence, not a guessed schedule.
        self.assertIsNone(quota.get_digest_cadence(uid))

    def test_infra_error_fails_open(self):
        uid = str(self.user.id)
        with mock.patch.object(quota, "_user_cfg", side_effect=RuntimeError("redis down")):
            self.assertEqual(quota.get_agent_tool_level(uid), "organize")
            self.assertEqual(quota.get_max_effort(uid), "high")
            self.assertTrue(quota.get_auto_effort(uid))
            self.assertEqual(quota.get_agent_memory(uid), "team")
            self.assertIsNone(quota.get_agent_history_retention_days(uid))
            self.assertEqual(
                quota.get_integrations(uid), ["web", "google_calendar", "github"]
            )
            self.assertIsNone(quota.get_digest_cadence(uid))

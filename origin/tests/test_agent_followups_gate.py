"""The gate of the parity batch's model-visible follow-ups: `AGENT_PARITY_FOLLOWUPS` (MIP-1589, MIP-1607, MIP-1590).

`AGENT_PARITY_TOOLS` is on in production since 2026-11-15 and can no longer protect a new model-visible change, so
the follow-ups have their own flag, default OFF. This file pins the mechanism the three items share; each item's own
tests pin what it does with the flag on and off. The committed fingerprint golden (`test_agent_fingerprint_golden`)
is the proof that with the flag off the model is shown exactly what it was.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import settings as production
from apis import settings_test as test_settings
from origin.search_engine.agent import parity
from origin.search_engine.agent.evals.group import harness

FLAG = "AGENT_PARITY_FOLLOWUPS"


class FlagDefaultTests(SimpleTestCase):
    def test_it_ships_off(self):
        self.assertIs(production.SEARCH_ENGINE[FLAG], False)

    def test_the_test_suite_runs_with_it_off(self):
        self.assertIs(settings.SEARCH_ENGINE[FLAG], False)
        # Not one of the flags the suite switches off for production: it is off in production too.
        self.assertNotIn(FLAG, test_settings.AGENT_FLAGS_ON_IN_PRODUCTION)

    def test_the_helper_follows_the_setting(self):
        self.assertEqual(parity.FOLLOWUPS_FLAG, FLAG)
        self.assertFalse(parity.followups_on())
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True}):
            self.assertTrue(parity.followups_on())
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: False}):
            self.assertFalse(parity.followups_on())

    def test_it_is_independent_of_the_parity_flag(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {parity.PARITY_FLAG: True}):
            self.assertFalse(parity.followups_on())
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True, parity.PARITY_FLAG: False}):
            self.assertTrue(parity.followups_on())
            self.assertFalse(parity.parity_on())


class ArmTests(SimpleTestCase):
    def test_the_arm_turns_on_only_the_new_flag(self):
        arms = harness.load_arms()
        self.assertIn("followups", arms)
        self.assertEqual(arms["followups"].effective, {FLAG: True})

    def test_the_all_on_arm_includes_it(self):
        self.assertTrue(harness.all_on_arm(harness.load_arms()).effective[FLAG])

    def test_the_arm_changes_something_against_the_baseline(self):
        # The baseline (arm0) is what production runs: the flag off.
        self.assertEqual(harness.validate_overrides(harness.load_arms()["followups"], production.SEARCH_ENGINE), [])

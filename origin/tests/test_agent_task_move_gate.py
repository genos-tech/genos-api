"""The gate of MIP-1609's model-visible change: `AGENT_TASK_MOVE_PARITY` (ON by default since 2026-11-16).

`AGENT_PARITY_TOOLS` and `AGENT_PARITY_FOLLOWUPS` are both on in production since 2026-11-15, so neither can protect
a new model-visible change: `update_task`'s project move refusing an assignee outside the destination and saying
what the move did has its own flag. It shipped OFF and is ON by default since the 2026-11-16 flip (env `false` is the rollback); the
key-free test settings keep it OFF (`apis/settings_test.py`), so the "off" tests describe the rollback arm, not
production. This file pins the mechanism; `test_agent_task_move_parity` pins what the tool
does with the flag on and off. The committed fingerprint golden (`test_agent_fingerprint_golden`) is the proof that
no declaration or prompt moves: the flag changes tool RESULTS only.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import settings as production
from apis import settings_test as test_settings
from origin.search_engine.agent import parity
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.evals.group import harness

FLAG = "AGENT_TASK_MOVE_PARITY"


class FlagDefaultTests(SimpleTestCase):
    def test_it_ships_on(self):
        # The production default (`apis.settings`); the rollback is the env value `false`.
        self.assertIs(production.SEARCH_ENGINE[FLAG], True)

    def test_the_test_suite_runs_with_it_off_and_it_is_a_shipped_flag(self):
        # Every other test assumes the unflagged behaviour and turns the flag on explicitly; the "off" tests
        # of the flag therefore describe the rollback arm.
        self.assertIs(settings.SEARCH_ENGINE[FLAG], False)
        self.assertIn(FLAG, test_settings.AGENT_FLAGS_ON_IN_PRODUCTION)

    def test_the_helper_follows_the_setting(self):
        self.assertEqual(parity.TASK_MOVE_FLAG, FLAG)
        self.assertFalse(parity.task_move_on())
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True}):
            self.assertTrue(parity.task_move_on())

    def test_it_is_independent_of_the_two_batch_flags(self):
        with mock.patch.dict(
            settings.SEARCH_ENGINE, {parity.PARITY_FLAG: True, parity.FOLLOWUPS_FLAG: True}
        ):
            self.assertFalse(parity.task_move_on())
        with mock.patch.dict(
            settings.SEARCH_ENGINE,
            {FLAG: True, parity.PARITY_FLAG: False, parity.FOLLOWUPS_FLAG: False},
        ):
            self.assertTrue(parity.task_move_on())


class DeclarationsDoNotMoveTests(SimpleTestCase):
    def test_the_tool_declarations_are_the_same_with_the_flag_on(self):
        # The flag changes a tool result and adds a refusal; no description, no parameter.
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: False}):
            off = [(d.name, d.description, d.parameters_schema) for d in _build_tool_declarations()]
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True}):
            on = [(d.name, d.description, d.parameters_schema) for d in _build_tool_declarations()]
        self.assertEqual(off, on)


class ArmTests(SimpleTestCase):
    def test_the_arm_turns_on_only_the_new_flag(self):
        arms = harness.load_arms()
        self.assertIn("task-move-parity", arms)
        self.assertEqual(arms["task-move-parity"].effective, {FLAG: True})

    def test_the_all_on_arm_includes_it(self):
        self.assertTrue(harness.all_on_arm(harness.load_arms()).effective[FLAG])

    def test_the_arm_was_measured_against_the_rollback(self):
        # The baseline the arm was measured against is the rollback: the flag off. Against today's production
        # the flag is already on, and the harness says the arm would change nothing.
        rollback = {**production.SEARCH_ENGINE, FLAG: False}
        arm = harness.load_arms()["task-move-parity"]
        self.assertEqual(harness.validate_overrides(arm, rollback), [])
        self.assertEqual(len(harness.validate_overrides(arm, production.SEARCH_ENGINE)), 1)

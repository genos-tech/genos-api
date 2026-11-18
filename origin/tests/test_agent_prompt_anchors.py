"""The two flag-gated lines spliced into the agent's system prompt, and what a moved anchor does (MIP-1658).

`agent_system_prompt()` adds one line after rule 7 (`AGENT_REJECT_REASON`, MIP-889) and one after the
to-do citation shape (`AGENT_PARITY_TOOLS`, MIP-1059) by replacing a sentence it knows. Both flags ship
ON. When someone rewords that sentence, the old splice RAISED on every ask: a 500 on the whole agent for
an edit of one sentence of prose, which CI saw only as a couple of dozen unrelated errors.

Now:
  * ONE test names the sentence that moved (`test_the_anchor_*`): that is where a wording edit shows up;
  * production does not raise: the prompt is served without the flag's line, and the miss is logged
    once per process with the sentence's name;
  * what the model is sent, with the anchors in place, is unchanged (the existing pins).
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent import prompts
from origin.search_engine.agent.prompts import (
    AGENT_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT_REGISTRY,
    agent_system_prompt,
)

BOTH_ON = {
    "AGENT_REJECT_REASON": True,
    "AGENT_PARITY_TOOLS": True,
    "AGENT_CHEATSHEET_FROM_REGISTRY": False,
}
BOTH_OFF = {**BOTH_ON, "AGENT_REJECT_REASON": False, "AGENT_PARITY_TOOLS": False}


def _prompt(flags: dict) -> str:
    with mock.patch.dict(settings.SEARCH_ENGINE, flags):
        return agent_system_prompt()


class TheAnchorsAreInThePrompt(SimpleTestCase):
    """The one place that fails, naming the sentence, when a wording edit moves an anchor."""

    def test_the_anchor_of_rule_7_is_in_both_prompts(self):
        for name, text in (
            ("AGENT_SYSTEM_PROMPT", AGENT_SYSTEM_PROMPT),
            ("AGENT_SYSTEM_PROMPT_REGISTRY", AGENT_SYSTEM_PROMPT_REGISTRY),
        ):
            # assertTrue, not assertIn: a failing assertIn would print the whole 40 KB prompt.
            self.assertTrue(
                prompts._RULE_7 in text,
                f"rule 7 moved in {name}: update `_RULE_7` in prompts.py (MIP-889, "
                "AGENT_REJECT_REASON) to the sentence as it now reads, or the flag's line is "
                "silently NOT added in production",
            )

    def test_the_anchor_of_the_todo_shape_is_in_both_prompts(self):
        for name, text in (
            ("AGENT_SYSTEM_PROMPT", AGENT_SYSTEM_PROMPT),
            ("AGENT_SYSTEM_PROMPT_REGISTRY", AGENT_SYSTEM_PROMPT_REGISTRY),
        ):
            self.assertTrue(
                prompts._TODO_SHAPE_END in text,
                f"the to-do citation shape moved in {name}: update `_TODO_SHAPE_END` in "
                "prompts.py (MIP-1059, AGENT_PARITY_TOOLS) to the line as it now reads, or "
                "the flag's line is silently NOT added in production",
            )


class WithTheAnchorsInPlace(SimpleTestCase):
    def test_both_flags_add_their_line_once_and_nothing_else(self):
        on = _prompt(BOTH_ON)
        off = _prompt(BOTH_OFF)
        self.assertEqual(on.count(prompts._RULE_7_WITH_REASON), 1)
        self.assertEqual(on.count(prompts._TODO_SHAPE_END_WITH_EVENT), 1)
        undone = on.replace(prompts._RULE_7_WITH_REASON, prompts._RULE_7, 1).replace(
            prompts._TODO_SHAPE_END_WITH_EVENT, prompts._TODO_SHAPE_END, 1
        )
        self.assertEqual(undone, off)
        self.assertEqual(off, AGENT_SYSTEM_PROMPT)


class WhenAnAnchorHasMoved(SimpleTestCase):
    """A reworded sentence is a degraded prompt and one log line, never an error on every ask."""

    def setUp(self):
        prompts._ANCHORS_REPORTED.clear()
        self.addCleanup(prompts._ANCHORS_REPORTED.clear)

    def test_a_moved_rule_7_does_not_raise_and_the_line_is_left_out(self):
        with mock.patch.object(prompts, "_RULE_7", "  7. A sentence that is not in the prompt.\n"):
            with self.assertLogs(prompts.logger, level="ERROR") as logs:
                served = _prompt(BOTH_ON)
        self.assertNotIn(prompts._RULE_7_WITH_REASON, served)
        self.assertIn(prompts._TODO_SHAPE_END_WITH_EVENT, served)  # the other splice is unaffected
        self.assertEqual(len(logs.records), 1)
        self.assertIn("rule 7", logs.output[0])

    def test_a_moved_todo_shape_does_not_raise_and_the_line_is_left_out(self):
        with mock.patch.object(prompts, "_TODO_SHAPE_END", "a line that is not in the prompt\n"):
            with self.assertLogs(prompts.logger, level="ERROR") as logs:
                served = _prompt(BOTH_ON)
        self.assertNotIn(prompts._TODO_SHAPE_END_WITH_EVENT, served)
        self.assertIn(prompts._RULE_7_WITH_REASON, served)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("to-do citation shape", logs.output[0])

    def test_the_miss_is_logged_once_per_process_not_once_per_ask(self):
        with mock.patch.object(prompts, "_RULE_7", "  7. Not in the prompt.\n"):
            with self.assertLogs(prompts.logger, level="ERROR") as logs:
                for _ in range(5):
                    _prompt(BOTH_ON)
        self.assertEqual(len(logs.records), 1)

    def test_both_moved_means_both_named_and_the_flag_off_prompt_served(self):
        with (
            mock.patch.object(prompts, "_RULE_7", "  7. Not in the prompt.\n"),
            mock.patch.object(prompts, "_TODO_SHAPE_END", "not in the prompt either\n"),
        ):
            with self.assertLogs(prompts.logger, level="ERROR") as logs:
                served = _prompt(BOTH_ON)
        self.assertEqual(served, _prompt(BOTH_OFF))
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(any("rule 7" in line for line in logs.output))
        self.assertTrue(any("to-do citation shape" in line for line in logs.output))

    def test_the_registry_generated_prompt_degrades_the_same_way(self):
        flags = {**BOTH_ON, "AGENT_CHEATSHEET_FROM_REGISTRY": True}
        with mock.patch.object(prompts, "_RULE_7", "  7. Not in the prompt.\n"):
            with self.assertLogs(prompts.logger, level="ERROR"):
                served = _prompt(flags)
        self.assertNotIn(prompts._RULE_7_WITH_REASON, served)

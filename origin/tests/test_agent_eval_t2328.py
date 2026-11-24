"""The grouped-eval pieces of MIP-2329 (arm `t2328`, case file `t2328-help.yaml`). NO model runs.

Ken decided on 2026-11-24 (user-guide milestone MIP-2304) that there is no paid eval for the help routing, so
these are schema and loader tests only: the arm is valid and gates the flag the tool is declared by, every case
exists in both languages, the positives wait for the arm and the negatives run on arm0 too, and every tool a
case names exists. Nothing here calls a model.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness
from origin.search_engine.agent.tools import REGISTRY

FLAG = "AGENT_INAPP_HELP_TOOL"
TOOL = "get_genos_help"


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t2328" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()

    def test_twenty_routing_cases_in_both_languages(self):
        self.assertEqual(len(self.cases), 20)
        self.assertEqual({c.kind for c in self.cases}, {"routing"})
        self.assertEqual(len({c.id for c in self.cases}), 20)
        for lang in ("en", "ja"):
            self.assertEqual(len([c for c in self.cases if c.lang == lang]), 10, lang)

    def test_every_english_case_has_a_japanese_twin_asked_in_japanese(self):
        slugs = {
            lang: {c.id.split(f"-{lang}-", 1)[1] for c in self.cases if c.lang == lang}
            for lang in ("en", "ja")
        }
        self.assertEqual(slugs["en"], slugs["ja"])
        for c in self.cases:
            has_ja = bool(re.search(r"[぀-ヿ㐀-鿿]", c.raw["query"]))
            self.assertEqual(has_ja, c.lang == "ja", c.id)

    def test_positives_wait_for_the_arm_and_negatives_run_on_arm0_too(self):
        positives = [c for c in self.cases if TOOL in c.raw["ok"]]
        negatives = [c for c in self.cases if TOOL not in c.raw["ok"]]
        self.assertEqual(len(positives), 12)
        self.assertEqual(len(negatives), 8)
        for c in positives:
            self.assertEqual(c.requires_arm, "t2328", c.id)
            self.assertEqual(c.raw["ok"], [TOOL], c.id)
        for c in negatives:
            self.assertIsNone(c.requires_arm, c.id)

    def test_every_tool_a_case_names_exists(self):
        for c in self.cases:
            for name in c.raw["ok"]:
                self.assertTrue(name in REGISTRY or name == TOOL, f"{c.id}: {name}")


class ArmTests(SimpleTestCase):
    def test_the_arm_overrides_exactly_the_flag_the_tool_is_gated_on(self):
        arm = harness.load_arms()["t2328"]
        self.assertEqual(arm.effective, {FLAG: True})
        self.assertEqual(arm.requires, [])
        self.assertEqual(harness.validate_overrides(arm), [])

    def test_the_all_on_arm_carries_it(self):
        self.assertIs(harness.all_on_arm(harness.load_arms()).effective[FLAG], True)

    def test_the_plan_runs_negatives_on_both_arms_and_positives_only_on_the_arm(self):
        plan = harness.build_plan(
            harness.load_cases(include_legacy=False), harness.load_arms(), arm_names=["t2328"]
        )
        ran = {(i.arm.name, i.case.id) for i in plan.items if not i.skip}
        self.assertIn(("arm0", "t2328-en-find-task"), ran)
        self.assertIn(("t2328", "t2328-en-find-task"), ran)
        self.assertIn(("t2328", "t2328-en-invite"), ran)
        self.assertNotIn(("arm0", "t2328-en-invite"), ran)

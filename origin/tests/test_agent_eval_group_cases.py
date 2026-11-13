"""Every shipped eval-group case file: structure, so a typo cannot quietly weaken a case.

A misspelled `expect:` key is ignored by the runner (it only reads the keys it knows), which turns a
check into nothing and a regression into a pass. These tests catch that, for every file in
`evals/group/`, and pin the shape of the t1271 / t1281 sets.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import harness, sequence

GROUP_DIR = Path(harness.__file__).parent


def runner_expect_keys() -> set[str]:
    source = inspect.getsource(runner._check_behavior_expectations)
    return set(re.findall(r'"([a-z_]+)" in expect', source)) | set(
        re.findall(r'expect\.get\("([a-z_]+)"\)', source)
    )


class GroupCaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = harness.load_cases(include_legacy=False)

    def test_there_are_group_cases_and_ids_are_unique(self):
        ids = [c.id for c in self.cases]
        self.assertTrue(ids)
        self.assertEqual([i for i, n in Counter(ids).items() if n > 1], [])

    def test_every_case_has_a_language_the_group_tag_and_an_item_tag(self):
        for c in self.cases:
            self.assertIn(c.lang, ("en", "ja"), c.id)
            self.assertIn("eval-group", c.tags, c.id)
            self.assertGreaterEqual(len(c.tags), 2, f"{c.id} has no item tag")

    def test_expect_keys_are_all_known_to_the_runner_or_the_group_layer(self):
        known = runner_expect_keys() | harness.GROUP_EXPECT_KEYS
        self.assertIn("tools_used_contains", known)  # the regexp found the runner's keys
        for c in self.cases:
            if c.kind != "behaviour":
                continue
            expect = c.raw.get("expect") or {}
            for turn in c.raw.get("turns") or []:
                expect = {**expect, **(turn.get("expect") or {})}
            self.assertEqual(sorted(set(expect) - known), [], f"{c.id}: unknown expect key")

    def test_sequence_cases_name_a_real_viewer(self):
        for c in self.cases:
            if c.fixture == "sequence":
                self.assertIn(c.raw.get("viewer", "v1"), sequence.VIEWERS, c.id)


class T1271Tests(SimpleTestCase):
    def setUp(self):
        self.cases = [c for c in harness.load_cases(include_legacy=False) if "t1271" in c.tags]
        self.t1281 = [c for c in harness.load_cases(include_legacy=False) if "t1281" in c.tags]

    def test_en_and_ja_each_have_a_positive_a_negative_and_an_edge_case(self):
        for lang in ("en", "ja"):
            ids = {c.id for c in self.cases if c.lang == lang}
            self.assertGreaterEqual(len(ids), 6)
            for needle in (
                "what-happened",
                "who-waits",
                "what-changed",
                "client-v2-no-leak",
                "find-task",
                "my-day",
            ):
                self.assertTrue(any(needle in i for i in ids), f"{lang}: no {needle}")

    def test_every_case_runs_on_the_sequence_fixture(self):
        self.assertTrue(all(c.fixture == "sequence" for c in self.cases + self.t1281))

    def test_japanese_cases_are_asked_in_japanese(self):
        for c in self.cases:
            has_ja = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", c.raw["query"]))
            self.assertEqual(has_ja, c.lang == "ja", c.id)

    def test_the_v2_cases_check_the_answer_and_the_tool_results_for_a_leak(self):
        v2 = [c for c in self.cases + self.t1281 if c.raw.get("viewer") == "v2"]
        self.assertEqual(len(v2), 4)
        for c in v2:
            expect = c.raw["expect"]
            self.assertTrue(expect["answer_does_not_contain"])
            self.assertEqual(
                expect["no_tool_results_contain"][:4],
                ["missing permission", "csv", "shipped", "reproduced"],
            )

    def test_the_t1281_variants_wait_for_the_timeline_tool(self):
        self.assertEqual(len(self.t1281), len(self.cases))
        for c in self.t1281:
            self.assertEqual(c.requires_arm, "t1289", c.id)
        story = [
            c
            for c in self.t1281
            if c.id.endswith(("what-happened", "what-changed", "full-timeline"))
        ]
        for c in story:
            self.assertEqual(c.raw["expect"]["tools_used_contains"], ["get_timeline"], c.id)

    def test_the_t1271_cases_do_not_expect_a_tool_that_does_not_exist(self):
        from origin.search_engine.agent.tools import REGISTRY

        for c in self.cases:
            expect = c.raw["expect"]
            for key in ("tools_used_contains", "tools_used_contains_any", "tools_used_excludes"):
                for name in expect.get(key, []):
                    self.assertIn(name, REGISTRY, f"{c.id}: {name}")
        for c in self.t1281:
            for name in c.raw["expect"].get("tools_used_contains_any", []) + c.raw["expect"].get(
                "tools_used_excludes", []
            ):
                self.assertTrue(name in REGISTRY or name == "get_timeline", f"{c.id}: {name}")

    def test_order_facts_carry_alternatives_in_both_languages(self):
        for c in self.cases:
            for fact in c.raw["expect"].get("facts_in_order", []):
                self.assertIsInstance(fact, list, c.id)
                self.assertEqual(
                    [f for f in fact if f != f.lower()], [], f"{c.id}: facts match lower-case"
                )

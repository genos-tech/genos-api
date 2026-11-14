"""The grouped-eval pieces of MIP-1561 (arm `parity`, case files `t1561-*.yaml`). No model runs.

The paid session measures the parity batch in ONE run, so what has to be right before it is the
files: the arm names the flag the parameters are gated on, every case exists in both languages,
the positives need the arm (they cannot pass without the parameters being declared) and the
negatives run on arm0 too (the baseline), and the new `pending_write` argument checks judge what
they say they judge.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import content_checks, harness

FLAG = "AGENT_PARITY_TOOLS"
FEATURES = (
    "status",
    "status-plan",
    "template-bug",
    "template-plan",
    "links-update",
    "links-create",
    "reporter-create",
    "reporter-update",
)
NEGATIVES = ("plain", "template-none", "links-body", "assign-not-reporter")


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1561" in c.tags]


class ArmTests(SimpleTestCase):
    def test_the_arm_turns_on_the_one_parity_flag(self):
        arms = harness.load_arms()
        self.assertIn("parity", arms)
        self.assertEqual(arms["parity"].effective, {FLAG: True})

    def test_the_all_on_arm_includes_it(self):
        self.assertTrue(harness.all_on_arm(harness.load_arms()).effective[FLAG])

    def test_the_flag_is_a_real_setting_that_ships_off(self):
        from apis import settings as production  # noqa: PLC0415

        self.assertIn(FLAG, production.SEARCH_ENGINE)
        self.assertIs(production.SEARCH_ENGINE[FLAG], False)


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_feature_and_negative_is_there_in_both_languages(self):
        for slug in (*FEATURES, *NEGATIVES):
            for lang in ("en", "ja"):
                self.assertTrue(
                    any(c.id.startswith(f"t1561.{slug}.") and c.lang == lang for c in self.cases),
                    f"{slug}.{lang}",
                )

    def test_ids_are_unique_and_each_language_has_a_twin(self):
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            twin = c.id[:-2] + ("ja" if c.lang == "en" else "en")
            self.assertIn(twin, self.by_id, c.id)

    def test_japanese_cases_are_asked_in_japanese(self):
        for c in self.cases:
            if c.lang == "ja":
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)

    def test_positives_need_the_arm_and_negatives_run_on_arm0(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in NEGATIVES:
                self.assertFalse(c.requires_arm, c.id)
            else:
                self.assertEqual(c.requires_arm, "parity", c.id)

    def test_every_case_judges_a_proposal_not_an_answer(self):
        for c in self.cases:
            expect = c.raw["expect"]
            self.assertTrue(expect.get("pending_tools_contains"), c.id)
            if c.id.split(".")[1] != "assign-not-reporter":
                self.assertIn("pending_write", expect, c.id)

    def test_every_check_key_is_one_the_harness_knows(self):
        known = {
            "tool",
            "tasks_at_least",
            "args_present",
            "args_absent",
            "args_match",
            "task_args_match",
        }
        for c in self.cases:
            spec = c.raw["expect"].get("pending_write") or {}
            self.assertFalse(set(spec) - known, c.id)

    def test_every_pattern_compiles_and_names_what_it_judges(self):
        for c in self.cases:
            spec = c.raw["expect"].get("pending_write") or {}
            for key in ("args_match", "task_args_match"):
                for name, pattern in (spec.get(key) or {}).items():
                    re.compile(pattern)
                    self.assertTrue(name, c.id)

    def test_the_plain_negatives_forbid_every_parity_argument_of_the_tool(self):
        spec = self.by_id["t1561.plain.en"].raw["expect"]["pending_write"]
        self.assertEqual(set(spec["args_absent"]), {"status", "reporter_id", "links", "template"})


class CheckPendingTests(SimpleTestCase):
    def check(self, spec, arguments, tool="create_task"):
        out = content_checks.check_pending(
            {"tool": tool, **spec}, [{"tool_name": tool, "arguments": arguments}]
        )
        return {c["name"]: c["ok"] for c in out}

    def test_args_present(self):
        self.assertTrue(
            self.check({"args_present": ["status"]}, {"status": "WIP"})[
                "pending_write.args_present"
            ]
        )
        for sent in ({}, {"status": None}, {"status": ""}):
            self.assertFalse(
                self.check({"args_present": ["status"]}, sent)["pending_write.args_present"]
            )

    def test_args_absent(self):
        self.assertTrue(
            self.check({"args_absent": ["status", "links"]}, {"title": "T"})[
                "pending_write.args_absent"
            ]
        )
        self.assertFalse(
            self.check({"args_absent": ["status"]}, {"status": "WIP"})["pending_write.args_absent"]
        )

    def test_args_match_is_a_case_insensitive_search_on_the_value_as_text(self):
        self.assertTrue(
            self.check({"args_match": {"status": "^wip$"}}, {"status": "WIP"})[
                "pending_write.args_match"
            ]
        )
        self.assertFalse(
            self.check({"args_match": {"status": "^WIP$"}}, {"status": "Open"})[
                "pending_write.args_match"
            ]
        )
        links = {"links": [{"url": "https://web.dev/articles/inp", "title": "INP"}]}
        self.assertTrue(
            self.check({"args_match": {"links": r"web\.dev/articles/inp"}}, links)[
                "pending_write.args_match"
            ]
        )
        self.assertFalse(self.check({"args_match": {"links": "x"}}, {})["pending_write.args_match"])

    def test_task_args_match_wants_at_least_one_task_to_match_each(self):
        plan = {"tasks": [{"title": "A", "status": "Closed"}, {"title": "B"}, "nonsense"]}
        self.assertTrue(
            self.check({"task_args_match": {"status": "^Closed$"}}, plan, "create_task_plan")[
                "pending_write.task_args_match"
            ]
        )
        self.assertFalse(
            self.check({"task_args_match": {"status": "^WIP$"}}, plan, "create_task_plan")[
                "pending_write.task_args_match"
            ]
        )

    def test_a_case_without_the_keys_gets_no_new_check(self):
        out = self.check({}, {"title": "T"})
        self.assertEqual(set(out), {"pending_write"})

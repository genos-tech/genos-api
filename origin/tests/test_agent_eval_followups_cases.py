"""The grouped-eval pieces of the follow-ups flag (arm `followups`, case files `t1589-*`, and the others that
join it): the shape of the case files, no model.

One paid run measures the follow-ups, so what has to be right before it is the files: the arm names the new flag,
every case exists in both languages, the positives need the arm (they cannot pass without the flag's behaviour) and
the controls run on arm0 too AND carry the arm's tag, or they are controls that never run with the flag on
(`test_agent_eval_arm_coverage`), and the seeds the cases name exist.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness, seeds

ARM = "followups"
FLAG = "AGENT_PARITY_FOLLOWUPS"
ITEM_TAGS = ("t1589", "t1590", "t1607")
WRITES = {"create_task", "create_task_plan"}


def cases(tag=None):
    wanted = (tag,) if tag else ITEM_TAGS
    return [c for c in harness.load_cases(include_legacy=False) if set(wanted) & set(c.tags)]


class ArmTests(SimpleTestCase):
    def test_the_arm_turns_on_only_the_followups_flag(self):
        self.assertEqual(harness.load_arms()[ARM].effective, {FLAG: True})

    def test_every_followups_case_file_has_a_positive_and_a_control(self):
        for tag in ITEM_TAGS:
            found = cases(tag)
            if not found and tag != "t1589":
                continue  # the items are separate PRs of one stack: judged where their files exist
            self.assertTrue(found, tag)
            self.assertTrue(any(c.requires_arm == ARM for c in found), f"{tag}: no positive")
            self.assertTrue(any(not c.requires_arm for c in found), f"{tag}: no control")


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

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

    def test_positives_need_the_arm_and_controls_run_on_arm0_with_the_arms_tag(self):
        for c in self.cases:
            if c.requires_arm:
                self.assertEqual(c.requires_arm, ARM, c.id)
            else:
                self.assertIn(ARM, c.tags, f"{c.id}: a control must carry the arm's tag")

    def test_every_case_is_in_the_eval_group_with_its_item_tag(self):
        for c in self.cases:
            self.assertIn(harness.GROUP_TAG, c.tags, c.id)
            self.assertTrue(set(ITEM_TAGS) & set(c.tags), c.id)

    def test_every_seed_a_case_names_exists(self):
        for c in self.cases:
            for name in c.raw.get("seed") or []:
                self.assertIn(name, seeds.SEEDS, c.id)

    def test_every_case_says_what_it_judges(self):
        for c in self.cases:
            self.assertTrue(str(c.raw.get("note") or "").strip(), c.id)
            self.assertTrue(c.raw["expect"], c.id)


class FieldRuleCaseTests(SimpleTestCase):
    """`t1589-field-rules.yaml`."""

    def setUp(self):
        self.cases = {c.id: c for c in cases("t1589")}

    def test_every_slug_is_there_in_both_languages(self):
        for slug in ("missing-fields", "plan-missing", "list-rules", "given", "no-rules"):
            for lang in ("en", "ja"):
                self.assertIn(f"t1589.{slug}.{lang}", self.cases)

    def test_the_refusal_positives_forbid_a_create_proposal_and_name_the_fields(self):
        for lang in ("en", "ja"):
            for slug in ("missing-fields", "plan-missing"):
                expect = self.cases[f"t1589.{slug}.{lang}"].raw["expect"]
                self.assertEqual(set(expect["pending_tools_excludes"]), WRITES)
                self.assertTrue(expect["answer_contains_any"])

    def test_the_rules_positive_reads_them_with_list_project_tags(self):
        for lang in ("en", "ja"):
            expect = self.cases[f"t1589.list-rules.{lang}"].raw["expect"]
            self.assertEqual(expect["tools_used_contains"], ["list_project_tags"])
            self.assertEqual(set(expect["pending_tools_excludes"]), WRITES)

    def test_the_complete_create_control_must_still_reach_the_card_with_both_fields(self):
        for lang in ("en", "ja"):
            case = self.cases[f"t1589.given.{lang}"]
            self.assertFalse(case.requires_arm)
            spec = case.raw["expect"]["pending_write"]
            self.assertEqual(set(spec["args_present"]), {"priority", "due_date"})
            for pattern in spec["args_match"].values():
                re.compile(pattern)

    def test_the_cases_that_use_the_rules_project_seed_it(self):
        for c in self.cases.values():
            if c.raw.get("fixture") == "sequence":
                self.assertEqual(c.raw.get("seed"), ["project_field_rules"], c.id)

    def test_the_seed_makes_both_fields_required_without_a_default(self):
        self.assertEqual(
            seeds.FIELD_RULES, {"priority": {"required": True}, "dueDate": {"required": True}}
        )
        self.assertEqual(
            seeds.seed_texts("project_field_rules", "en"), [seeds.FIELD_RULES_PROJECT["en"]]
        )

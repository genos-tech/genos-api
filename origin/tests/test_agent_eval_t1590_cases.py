"""The grouped-eval pieces of MIP-1590 (arm `followups`, case file `t1590-mention-hardening.yaml`). No model runs.

The paid session measures the parity follow-ups in ONE run, so what has to be right before it is the files: the cases
exist in both languages, the positive needs the arm and the controls run on arm0 too (they carry the arm's tag, the
rule `test_agent_eval_arm_coverage` holds every arm to), every check key is one the harness knows, and the seed the cases
name puts the rows they point at in the database (the titles the mentions look up).
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness, seeds

SLUGS = ("deleted-project-task", "inject-title", "normal-mention")
POSITIVES = ("deleted-project-task",)
KNOWN_EXPECT = {
    "no_errors",
    "tools_used_contains",
    "tools_used_excludes",
    "pending_tools_excludes",
    "answer_contains_any",
    "answer_contains_all",
    "answer_does_not_contain",
    "answer_chars_at_least",
}


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1590" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_case_is_there_in_both_languages(self):
        self.assertEqual(len(self.cases), len(SLUGS) * 2)
        for slug in SLUGS:
            for lang in ("en", "ja"):
                self.assertIn(f"t1590.{slug}.{lang}", self.by_id)

    def test_each_case_has_its_twin_and_the_viewer_of_its_language(self):
        for c in self.cases:
            self.assertEqual(c.lang, c.id.rsplit(".", 1)[1], c.id)
            self.assertEqual(c.raw["viewer"], "b" if c.lang == "en" else "ja-b", c.id)
            self.assertEqual(c.raw["fixture"], "sequence", c.id)

    def test_japanese_cases_are_asked_in_japanese(self):
        for c in self.cases:
            if c.lang == "ja" and "normal-mention" not in c.id:
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)

    def test_the_positive_needs_the_arm_and_the_controls_run_on_arm0_with_the_arms_tag(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in POSITIVES:
                self.assertEqual(c.requires_arm, "followups", c.id)
            else:
                self.assertFalse(c.requires_arm, c.id)
                self.assertIn("followups", c.tags, c.id)

    def test_every_check_key_is_one_the_harness_knows(self):
        for c in self.cases:
            self.assertFalse(set(c.raw["expect"]) - KNOWN_EXPECT, c.id)

    def test_every_case_names_the_seed_and_a_mention_the_seed_makes(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug == "normal-mention":
                continue  # the story's own task
            self.assertEqual(c.raw["seed"], ["mention_hardening"], c.id)
            spec = seeds.MENTION_HARDENING[c.lang]
            (mention,) = c.raw["mentions"]
            wanted = spec["gone"] if slug == "deleted-project-task" else spec["hostile"]
            self.assertEqual(mention["lookup_title"], wanted, c.id)
            self.assertEqual(mention["label"], wanted, c.id)
            # The question carries the token the client writes: `#<label>`.
            self.assertIn("#" + wanted, c.raw["query"], c.id)

    def test_the_hostile_title_is_an_instruction_and_never_a_thing_the_answer_may_say(self):
        for lang in ("en", "ja"):
            spec = seeds.MENTION_HARDENING[lang]
            case = self.by_id[f"t1590.inject-title.{lang}"]
            for word in case.raw["expect"]["answer_does_not_contain"]:
                self.assertIn(word, spec["hostile"])

    def test_the_deleted_project_positive_forbids_the_facts_of_the_task_it_should_not_read(self):
        for lang in ("en", "ja"):
            body = seeds.MENTION_HARDENING[lang]["gone_body"]
            case = self.by_id[f"t1590.deleted-project-task.{lang}"]
            for word in case.raw["expect"]["answer_does_not_contain"]:
                self.assertIn(word, body)

    def test_the_deleted_project_cases_say_the_mention_is_expected_to_drop_and_no_other_does(self):
        # Without the key the runner raised "mention resolution dropped entries" before any model call (first paid
        # run); on any other case the key would hide a fixture that stopped resolving.
        for c in self.cases:
            self.assertEqual(
                bool(c.raw.get("mentions_expected_dropped")),
                c.id.startswith("t1590.deleted-project-task."),
                c.id,
            )

    def test_the_project_the_deleted_project_case_names_is_really_soft_deleted_by_its_seed(self):
        import inspect  # noqa: PLC0415

        source = inspect.getsource(seeds.seed_mention_hardening)
        self.assertIn("is_deleted=True", source)

    def test_no_case_may_end_on_a_write_proposal(self):
        for c in self.cases:
            if "normal-mention" not in c.id:
                self.assertTrue(c.raw["expect"].get("pending_tools_excludes"), c.id)


class ArmTests(SimpleTestCase):
    def test_the_arm_runs_the_positive_and_the_controls_and_arm0_runs_the_controls_only(self):
        selected = harness.select_cases(cases())
        plan = harness.build_plan(selected, harness.load_arms(), arm_names=["followups"])
        on = sorted(i.case.id for i in plan.runnable() if i.arm.name == "followups")
        base = sorted(i.case.id for i in plan.runnable() if i.arm.is_baseline)
        self.assertEqual(len(on), 6)
        self.assertEqual(len(base), 4)
        self.assertEqual(
            sorted(set(on) - set(base)), [f"t1590.deleted-project-task.{x}" for x in ("en", "ja")]
        )

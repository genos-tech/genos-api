"""The grouped-eval pieces of MIP-1561 (arm `parity`, case files `t1561-*.yaml`). No model runs.

The paid session measures the parity batch in ONE run, so what has to be right before it is the
files: the arm names the flag the parameters are gated on, every case exists in both languages,
the positives need the arm (they cannot pass without the parameters being declared) and the
negatives run on arm0 too (the baseline), and the new `pending_write` argument checks judge what
they say they judge.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals.group import content_checks, harness
from origin.search_engine.agent.tools import ToolContext

from .test_base import BaseAPITestCase

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


class CardShapeTests(BaseAPITestCase):
    """`pending_write` judges the proposal AS THE APPROVAL CARD SHOWS IT (the
    `tool_call_pending_approval` event carries `_friendly_arguments`, not the model's raw call).
    The server fills a template body into `content_text` on that card, turns a reporter id into a
    username and so on, so a case that was written against the raw call can fail when the model
    did everything right. This builds the event arguments with the real card code, flag on, for a
    proposal each case should pass, and runs the case's own spec over them (and over one it must
    fail, so the check is not vacuous)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.by_id = {c.id: c for c in cases()}

    def verdict(self, case_id, tool, proposal):
        card = controller._friendly_arguments(
            {"project_id": self.project.project_id, **proposal}, tool, ctx=self.ctx
        )
        spec = self.by_id[case_id].raw["expect"]["pending_write"]
        out = content_checks.check_pending(spec, [{"tool_name": tool, "arguments": card}])
        return {c["name"]: c["ok"] for c in out}, card

    def assert_passes(self, case_id, tool, proposal):
        checks, card = self.verdict(case_id, tool, proposal)
        self.assertTrue(all(checks.values()), (case_id, checks, card))

    def test_the_template_case_passes_on_the_card_with_the_filled_body(self):
        proposal = {"title": "Search filter drops the last selected tag", "template": "bug"}
        for case_id in ("t1561.template-bug.en", "t1561.template-bug.ja"):
            with self.subTest(case=case_id):
                checks, card = self.verdict(case_id, "create_task", proposal)
                self.assertIn("### 🐞 Summary", card["content_text"])
                self.assertTrue(all(checks.values()), checks)

    def test_a_model_that_wrote_its_own_body_with_no_template_fails_the_template_case(self):
        checks, _ = self.verdict(
            "t1561.template-bug.en", "create_task", {"title": "T", "content_text": "my own"}
        )
        self.assertFalse(all(checks.values()))

    def test_the_plain_negatives_pass_with_the_default_body_on_the_card_and_fail_on_a_status(self):
        for case_id in ("t1561.plain.en", "t1561.plain.ja", "t1561.template-none.en"):
            with self.subTest(case=case_id):
                self.assert_passes(case_id, "create_task", {"title": "Buy more coffee"})
        checks, _ = self.verdict(
            "t1561.plain.en", "create_task", {"title": "Buy more coffee", "status": "WIP"}
        )
        self.assertFalse(checks["pending_write.args_absent"])

    def test_the_status_links_and_reporter_cases_pass_on_the_card(self):
        self.assert_passes("t1561.status.en", "create_task", {"title": "T", "status": "WIP"})
        self.assert_passes(
            "t1561.links-create.ja",
            "create_task",
            {"title": "T", "links": [{"url": "https://example.com/specs/pricing", "title": "P"}]},
        )
        # The card swaps the reporter's id for a username; the case asks only that one was sent.
        self.assert_passes(
            "t1561.reporter-create.en",
            "create_task",
            {"title": "T", "reporter_id": str(self.user2.id)},
        )

    def test_the_update_and_plan_cases_pass_on_the_card(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="T", status="Open"
        )
        self.assert_passes(
            "t1561.links-update.en",
            "update_task",
            {"task_id": task.task_id, "links": [{"url": "https://web.dev/articles/inp"}]},
        )
        self.assert_passes(
            "t1561.reporter-update.en",
            "update_task",
            {"task_id": task.task_id, "reporter_id": str(self.user2.id)},
        )
        self.assert_passes(
            "t1561.status-plan.en",
            "create_task_plan",
            {
                "tasks": [
                    {"title": "A", "status": "Closed"},
                    {"title": "B"},
                    {"title": "C"},
                ]
            },
        )
        self.assert_passes(
            "t1561.template-plan.ja",
            "create_task_plan",
            {"tasks": [{"title": "A", "template": "spike"}, {"title": "B"}]},
        )

"""The grouped-eval pieces of MIP-1564 (arm `parity`, case file `t1564-milestones.yaml`, seed `milestone_convert`).

No model runs. The paid session measures the parity batch in ONE run, so what has to be right before it
is the files: every case exists in both languages, the positives need the arm (they cannot pass without
the `convert` action and the `project_id` being declared) and the negatives run on arm0, the cases name
a viewer and fixture that exist, the seed writes the rows the cases quote under the display ids they
quote, and each case's own `pending_write` spec passes on the approval card the real card code builds
for a proposal it should accept, and fails on one it should not.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import content_checks, harness, seeds, sequence
from origin.search_engine.agent.tools import ToolContext

from .test_base import BaseAPITestCase

FLAG = "AGENT_PARITY_TOOLS"
POSITIVES = ("convert-to-milestone", "convert-to-task", "move-milestone")
NEGATIVES = ("show-not-convert", "favorite-no-tool")
UNCHANGED = ("move-task-update",)
SLUGS = (*POSITIVES, *NEGATIVES, *UNCHANGED)


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1564" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_case_is_there_in_both_languages_and_nothing_else(self):
        for slug in SLUGS:
            for lang in ("en", "ja"):
                self.assertIn(f"t1564.{slug}.{lang}", self.by_id)
        self.assertEqual(len(self.cases), 2 * len(SLUGS))

    def test_ids_are_unique_and_each_language_has_a_twin(self):
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            twin = c.id[:-2] + ("ja" if c.lang == "en" else "en")
            self.assertIn(twin, self.by_id, c.id)

    def test_japanese_cases_are_asked_in_japanese_and_by_the_japanese_viewer(self):
        for c in self.cases:
            if c.lang == "ja":
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)
                self.assertEqual(c.raw["viewer"], "ja-b", c.id)
            else:
                self.assertEqual(c.raw["viewer"], "b", c.id)

    def test_every_viewer_is_one_the_scenario_has_and_every_case_names_its_seed(self):
        for c in self.cases:
            self.assertIn(c.raw["viewer"], sequence.VIEWERS, c.id)
            self.assertEqual(c.raw["fixture"], "sequence", c.id)
            self.assertEqual(c.raw["seed"], ["milestone_convert"], c.id)

    def test_positives_need_the_arm_and_the_rest_run_on_arm0(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in POSITIVES:
                self.assertEqual(c.requires_arm, "parity", c.id)
            else:
                self.assertFalse(c.requires_arm, c.id)

    def test_every_check_key_is_one_the_harness_knows_and_every_pattern_compiles(self):
        known = {"tool", "args_present", "args_absent", "args_match", "task_args_match"}
        for c in self.cases:
            spec = (c.raw["expect"] or {}).get("pending_write")
            if spec is None:
                continue
            self.assertFalse(set(spec) - known, c.id)
            for pattern in (spec.get("args_match") or {}).values():
                re.compile(pattern)

    def test_the_positives_judge_the_proposal_and_the_negatives_forbid_every_write(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            expect = c.raw["expect"]
            if slug in POSITIVES:
                self.assertEqual(expect["pending_tools_contains"], ["manage_milestone"], c.id)
                self.assertEqual(expect["pending_write"]["tool"], "manage_milestone", c.id)
                self.assertNotIn("pending_tools_excludes", expect, c.id)
            elif slug in NEGATIVES:
                self.assertIn("manage_milestone", expect["pending_tools_excludes"], c.id)
                self.assertIn("update_task", expect["pending_tools_excludes"], c.id)
                self.assertGreaterEqual(expect["answer_chars_at_least"], 20, c.id)
                self.assertNotIn("pending_write", expect, c.id)
            else:
                self.assertEqual(expect["pending_tools_contains"], ["update_task"], c.id)
                self.assertEqual(expect["pending_tools_excludes"], ["manage_milestone"], c.id)

    def test_the_favorite_case_forbids_the_write_tools_a_model_might_reach_for(self):
        for lang in ("en", "ja"):
            excludes = self.by_id[f"t1564.favorite-no-tool.{lang}"].raw["expect"][
                "pending_tools_excludes"
            ]
            self.assertLessEqual(
                {"manage_milestone", "update_task", "update_tasks_bulk", "favorite_note"},
                set(excludes),
            )

    def test_a_move_judges_that_it_sends_none_of_what_a_move_clears(self):
        for lang in ("en", "ja"):
            absent = self.by_id[f"t1564.move-milestone.{lang}"].raw["expect"]["pending_write"][
                "args_absent"
            ]
            self.assertLessEqual({"sprint_id", "milestone_folder_id", "tags"}, set(absent))

    def test_every_case_names_the_item_by_its_display_id_too(self):
        # The seed writes rows only, so no search index has seen them: the display id is how a model
        # reaches an item without one.
        for c in self.cases:
            self.assertRegex(
                c.raw["query"].replace("（", "(").replace("）", ")"), r"\(EMC-\d\)", c.id
            )

    def test_the_cases_quote_only_titles_the_seed_has(self):
        for c in self.cases:
            texts = set(seeds.seed_texts("milestone_convert", c.lang))
            for quoted in re.findall(r'"([^"]+)"|「([^」]+)」', c.raw["query"]):
                text = quoted[0] or quoted[1]
                self.assertIn(text, texts, f"{c.id}: {text!r}")

    def test_the_seed_says_who_reads_it_and_has_text_in_both_languages(self):
        self.assertIn("milestone_convert", seeds.SEEDS)
        self.assertEqual(seeds.VISIBLE_TO["milestone_convert"], frozenset({"b"}))
        for lang in ("en", "ja"):
            self.assertEqual(len(seeds.seed_texts("milestone_convert", lang)), 8)


class SeedAndCardTests(BaseAPITestCase):
    """The seed writes the rows the cases name under the display ids they quote, and the approval
    card built for a proposal carries what `pending_write` is asked to match."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = {"team": "v1", "team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        self.ctx = ToolContext(team_id=self.info["team_id"], user_id=self.info["user_id"])
        self.by_id = {c.id: c for c in cases()}
        seeds.apply(["milestone_convert"], self.info)
        self.spec = seeds.MILESTONE_CASES["en"]

    def row(self, title):
        return TaskMaster.objects.get(team=self.team, title=title)

    def milestone(self, title):
        return MilestoneMaster.objects.get(team=self.team, title=title, is_deleted=False)

    def test_the_seed_writes_the_display_ids_the_cases_quote(self):
        self.assertEqual(self.row(self.spec["root"]).display_id, "EMC-1")
        self.assertEqual(self.milestone(self.spec["onboarding"]).task.display_id, "EMC-2")
        self.assertEqual(self.milestone(self.spec["spring"]).task.display_id, "EMC-5")
        for title in self.spec["onboarding_tasks"]:
            self.assertEqual(self.row(title).milestone.title, self.spec["onboarding"])
        for title in self.spec["spring_tasks"]:
            self.assertEqual(self.row(title).milestone.title, self.spec["spring"])

    def test_the_root_task_is_a_root_task_and_the_milestone_tasks_are_in_their_milestone(self):
        root = self.row(self.spec["root"])
        self.assertIsNone(root.parent_task_id)
        self.assertIsNone(root.milestone_id)
        self.assertFalse(root.is_milestone)
        child = self.row(self.spec["onboarding_tasks"][0])
        self.assertEqual(child.parent_task_id, self.milestone(self.spec["onboarding"]).task_id)

    def test_the_seed_is_idempotent_and_finishes_a_half_done_run(self):
        count = TaskMaster.objects.filter(team=self.team).count()
        seeds.apply(["milestone_convert"], self.info)
        self.assertEqual(TaskMaster.objects.filter(team=self.team).count(), count)
        TaskMaster.objects.filter(title=self.spec["onboarding_tasks"][1]).delete()
        seeds.apply(["milestone_convert"], self.info)
        self.assertEqual(TaskMaster.objects.filter(team=self.team).count(), count)
        self.assertEqual(
            TaskMaster.objects.filter(
                team=self.team, milestone=self.milestone(self.spec["onboarding"])
            ).count(),
            3,  # the two tasks and the milestone's own row
        )

    def test_the_viewer_is_alone_in_both_projects_and_nothing_is_assigned(self):
        for code in ("EMC", "EMA"):
            project = ProjectMaster.objects.get(code=code, team=self.team)
            self.assertEqual(
                set(
                    ProjectMembers.objects.filter(project=project).values_list(
                        "attendee_id", flat=True
                    )
                ),
                {self.user.id},
            )
        self.assertFalse(TaskMaster.objects.filter(team=self.team, assignee__isnull=False).exists())

    def check(self, case_id, tool, proposal):
        card = controller._friendly_arguments(proposal, tool, ctx=self.ctx)
        spec = self.by_id[case_id].raw["expect"]["pending_write"]
        out = content_checks.check_pending(spec, [{"tool_name": tool, "arguments": card}])
        return {c["name"]: c["ok"] for c in out}, card

    def test_convert_to_milestone_passes_on_the_right_proposal_and_fails_on_the_others(self):
        root = self.row(self.spec["root"])
        checks, card = self.check(
            "t1564.convert-to-milestone.en",
            "manage_milestone",
            {"action": "convert", "task_id": root.task_id},
        )
        self.assertTrue(all(checks.values()), (checks, card))
        # the wrong action, the wrong id argument and a sent project are each a failure
        for wrong in (
            {"action": "update", "task_id": root.task_id},
            {
                "action": "convert",
                "milestone_id": self.milestone(self.spec["onboarding"]).milestone_id,
            },
            {"action": "convert", "task_id": root.task_id, "project_id": 1},
        ):
            checks, _ = self.check("t1564.convert-to-milestone.en", "manage_milestone", wrong)
            self.assertFalse(all(checks.values()), wrong)

    def test_convert_to_task_passes_on_the_right_proposal_and_fails_on_the_other_direction(self):
        ms = self.milestone(self.spec["onboarding"])
        checks, card = self.check(
            "t1564.convert-to-task.en",
            "manage_milestone",
            {"action": "convert", "milestone_id": ms.milestone_id},
        )
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["current"]["direction"], "milestone_to_task")
        checks, _ = self.check(
            "t1564.convert-to-task.en",
            "manage_milestone",
            {"action": "convert", "task_id": self.row(self.spec["root"]).task_id},
        )
        self.assertFalse(all(checks.values()))

    def test_move_passes_on_the_right_proposal_and_fails_on_a_wrong_project_or_extra_fields(self):
        ms = self.milestone(self.spec["spring"])
        archive = ProjectMaster.objects.get(code="EMA", team=self.team)
        right = {
            "action": "update",
            "milestone_id": ms.milestone_id,
            "project_id": archive.project_id,
        }
        checks, card = self.check("t1564.move-milestone.en", "manage_milestone", right)
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["project_id"], "Eval archive")
        self.assertEqual(card["current"]["project_id"], "Eval milestones")
        for wrong in (
            {
                **right,
                "project_id": ProjectMaster.objects.get(code="EMC", team=self.team).project_id,
            },
            {**right, "sprint_id": "5"},
            {**right, "tags": ["Feature"]},
            {"action": "update", "milestone_id": ms.milestone_id, "title": "Renamed"},
        ):
            checks, _ = self.check("t1564.move-milestone.en", "manage_milestone", wrong)
            self.assertFalse(all(checks.values()), wrong)

    def test_move_task_passes_on_update_task_with_the_project_name(self):
        archive = ProjectMaster.objects.get(code="EMA", team=self.team)
        root = self.row(self.spec["root"])
        checks, card = self.check(
            "t1564.move-task-update.en",
            "update_task",
            {"task_id": root.task_id, "project_id": archive.project_id},
        )
        self.assertTrue(all(checks.values()), (checks, card))


def _stream(*, answer: str, proposed: tuple[str, ...] = (), used: tuple[str, ...] = ()):
    events = [{"type": "tool_call_start", "tool_name": name, "step": 0} for name in used]
    events += [
        {"type": "tool_call_pending_approval", "tool_name": name, "step": 1, "arguments": {}}
        for name in proposed
    ]
    events.append({"type": "answer_delta", "text": answer})
    return events


class ProposalCheckTests(SimpleTestCase):
    """A case that must not end in a write has to FAIL on a model that says it will do it and proposes
    one: the words before the call are streamed as the answer, so the answer alone cannot tell."""

    def test_every_negative_fails_on_a_run_that_proposed_a_milestone_write(self):
        said = "Done. Write the welcome email Record the demo video ウェルカムメールを書く デモ動画を録画する"
        for c in cases():
            slug = c.id.split(".")[1]
            if slug not in NEGATIVES:
                continue
            with self.subTest(case=c.id):
                bad = _stream(answer=said, proposed=("manage_milestone",))
                reasons = runner._check_behavior_expectations(bad, c.raw["expect"])
                self.assertTrue(
                    [r for r in reasons if r.startswith("pending_tools_excludes")], reasons
                )
                good = _stream(answer=said, used=("list_milestones",))
                self.assertFalse(
                    [
                        r
                        for r in runner._check_behavior_expectations(good, c.raw["expect"])
                        if r.startswith("pending_tools_excludes")
                    ]
                )

    def test_the_task_move_case_fails_a_model_that_reaches_for_the_milestone_tool(self):
        for c in cases():
            if c.id.split(".")[1] not in UNCHANGED:
                continue
            with self.subTest(case=c.id):
                bad = _stream(answer="Moved.", proposed=("manage_milestone", "update_task"))
                reasons = runner._check_behavior_expectations(bad, c.raw["expect"])
                self.assertTrue([r for r in reasons if r.startswith("pending_tools_excludes")])
                good = _stream(answer="Moved.", proposed=("update_task",))
                self.assertFalse(
                    [
                        r
                        for r in runner._check_behavior_expectations(good, c.raw["expect"])
                        if r.startswith("pending_tools_excludes")
                    ]
                )

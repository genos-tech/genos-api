"""The grouped-eval pieces of MIP-1563 (arm `parity`, case file `t1563-files.yaml`, seed `task_files`).

No model runs. The paid session measures the parity batch in ONE run, so what has to be right before it
is the files: every case exists in both languages, the positives need the arm (they cannot pass without
the `attachments` field and the delete tool being declared) and the negatives run on arm0 too, the cases
name a viewer and fixture that exist, the seed writes the rows the cases quote, and each case's own
`pending_write` spec passes on the approval card the real card code builds for a proposal it should
accept, and fails on one it should not.
"""

from __future__ import annotations

import re
import tempfile
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals.group import content_checks, harness, seeds, sequence
from origin.search_engine.agent.tools import ToolContext

from .test_base import BaseAPITestCase

FLAG = "AGENT_PARITY_TOOLS"
POSITIVES = ("list", "delete")
NEGATIVES = ("show-not-delete", "download-not-delete", "upload-not-delete")


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1563" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_positive_and_negative_is_there_in_both_languages(self):
        for slug in (*POSITIVES, *NEGATIVES):
            for lang in ("en", "ja"):
                self.assertIn(f"t1563.{slug}.{lang}", self.by_id)
        self.assertEqual(len(self.cases), 2 * (len(POSITIVES) + len(NEGATIVES)))

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
            self.assertEqual(c.raw["seed"], ["task_files"], c.id)

    def test_positives_need_the_arm_and_negatives_run_on_arm0(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in NEGATIVES:
                self.assertFalse(c.requires_arm, c.id)
            else:
                self.assertEqual(c.requires_arm, "parity", c.id)

    def test_every_check_key_is_one_the_harness_knows_and_every_pattern_compiles(self):
        known = {"tool", "args_present", "args_absent", "args_match", "task_args_match"}
        for c in self.cases:
            spec = (c.raw["expect"] or {}).get("pending_write")
            if spec is None:
                continue
            self.assertFalse(set(spec) - known, c.id)
            for pattern in (spec.get("args_match") or {}).values():
                re.compile(pattern)

    def test_a_negative_judges_the_answer_a_member_would_read(self):
        # A delete proposal pauses the run and leaves no answer, so a minimum answer length is what
        # fails a model that proposed one.
        for c in self.cases:
            if c.id.split(".")[1] in NEGATIVES:
                self.assertGreaterEqual(c.raw["expect"]["answer_chars_at_least"], 20, c.id)
                self.assertNotIn("pending_write", c.raw["expect"], c.id)

    def test_the_cases_quote_files_and_a_task_the_seed_has(self):
        for c in self.cases:
            lang = c.lang
            seed = seeds.TASK_FILES[lang]
            names = [name for name, _mime, _content in seed["files"]]
            for quoted in re.findall(r'"([^"]+)"|「([^」]+)」', c.raw["query"]):
                text = quoted[0] or quoted[1]
                self.assertEqual(text, seed["task"], f"{c.id}: {text!r}")
            if c.id.split(".")[1] in ("delete", "show-not-delete", "download-not-delete"):
                self.assertTrue(any(n in c.raw["query"] for n in names), c.id)

    def test_the_seed_says_who_reads_it_and_has_text_in_both_languages(self):
        self.assertIn("task_files", seeds.SEEDS)
        self.assertEqual(seeds.VISIBLE_TO["task_files"], frozenset({"b"}))
        for lang in ("en", "ja"):
            self.assertEqual(len(seeds.seed_texts("task_files", lang)), 3)


class SeedAndCardTests(BaseAPITestCase):
    """The seed writes the two files the cases name (ids 1 and 2), and the approval card built for a
    proposal carries what `pending_write` is asked to match."""

    def setUp(self):
        super().setUp()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = {"team": "v1", "team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        self.ctx = ToolContext(team_id=self.info["team_id"], user_id=self.info["user_id"])
        self.by_id = {c.id: c for c in cases()}

    def seed(self):
        # The seed's own project is made for the viewer in the viewer's team, as in an eval team.
        seeds.apply(["task_files"], self.info)
        return TaskMaster.objects.get(team=self.team, title=seeds.TASK_FILES["en"]["task"])

    def test_the_seed_writes_the_task_and_its_two_files_once(self):
        task = self.seed()
        self.assertEqual(
            list(
                TaskAttachments.objects.filter(task=task)
                .order_by("attachment_id")
                .values_list("attachment_id", "original_filename")
            ),
            [(1, "budget-2026.xlsx"), (2, "kickoff-notes.pdf")],
        )
        seeds.apply(["task_files"], self.info)  # idempotent
        self.assertEqual(TaskAttachments.objects.filter(task=task).count(), 2)
        project = ProjectMaster.objects.get(code="EAF", team=self.team)
        self.assertEqual(
            set(
                ProjectMembers.objects.filter(project=project).values_list("attendee_id", flat=True)
            ),
            {self.user.id},
        )
        self.assertIsNone(task.assignee_id)

    def test_the_seeded_files_are_real_files_with_a_size(self):
        task = self.seed()
        sizes = [a.attached_file.size for a in TaskAttachments.objects.filter(task=task)]
        self.assertTrue(all(size > 0 for size in sizes), sizes)

    def card(self, case_id, proposal):
        task = self.seed()
        card = controller._friendly_arguments(
            {"task_id": task.task_id, **proposal}, "delete_task_attachment", ctx=self.ctx
        )
        spec = self.by_id[case_id].raw["expect"]["pending_write"]
        out = content_checks.check_pending(
            spec, [{"tool_name": "delete_task_attachment", "arguments": card}]
        )
        return {c["name"]: c["ok"] for c in out}, card

    def test_the_delete_case_passes_on_the_right_file_and_fails_on_the_other(self):
        checks, card = self.card("t1563.delete.en", {"attachment_id": 2})
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["current"]["name"], "kickoff-notes.pdf")
        # the other file, and a call that never says which file, must not pass
        checks, _ = self.card("t1563.delete.en", {"attachment_id": 1})
        self.assertFalse(all(checks.values()))
        checks, _ = self.card("t1563.delete.en", {"attachment_id": 99})
        self.assertFalse(all(checks.values()))

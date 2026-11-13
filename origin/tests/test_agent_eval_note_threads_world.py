"""The t1090-notethreads cases against the REAL sequence world (no model, no OpenSearch).

The first paid run failed 8/8 on both arms because the model could not reach the seed's note: its id
is in no tool result of this world (`fetch_task` has no anchors with the 1288 flag off, the sequence
teams are not indexed, `list_*` do not list task notes). These tests build the world the way the runner
does (`case_runner.prepare_case`) and call the in-app tool directly, as the viewer of each case.
"""

from __future__ import annotations

import json
import re

from django.conf import settings
from django.test import TestCase, override_settings

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.evals.group import case_runner, harness, seeds, sequence
from origin.search_engine.agent.tools import REGISTRY, ToolContext

FLAG = "AGENT_NOTE_THREADS_IN_GET_NOTE"


def flag(value: bool):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: value})


class NoteThreadsWorldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        sequence.ensure_sequence()
        cls.cases = [
            c for c in harness.load_cases(include_legacy=False) if "t1090-notethreads" in c.tags
        ]

    def tearDown(self):
        seeds.remove_note_threads_stub()

    def prepared(self, case):
        raw = case_runner.prepare_case(case)
        match = re.search(r"note_id[^0-9]{0,6}(\d+)", raw["query"])
        self.assertIsNotNone(match, raw["query"])
        return raw, int(match.group(1))

    def test_there_are_eight_cases_and_each_query_names_the_seeds_note_id(self):
        self.assertEqual(len(self.cases), 8)
        for case in self.cases:
            raw, note_id = self.prepared(case)
            note = TaskNoteMaster.objects.get(note_id=note_id)
            self.assertEqual(str(note.team_id), raw["team_id"], case.id)
            self.assertNotIn("{", raw["query"], case.id)

    def test_each_cases_viewer_opens_the_note_and_the_flag_adds_the_threads(self):
        for case in self.cases:
            raw, note_id = self.prepared(case)
            ctx = ToolContext(team_id=raw["team_id"], user_id=raw["user_id"])
            args = {"note_type": "task", "note_id": note_id}
            with flag(False):
                off = REGISTRY["fetch_note"].run(args, ctx)
            with flag(True):
                on = REGISTRY["fetch_note"].run(args, ctx)
            self.assertNotIn("comment_threads", off, case.id)
            self.assertIn("バックアップ" if case.lang == "ja" else "backups", off["body_text"])
            (thread,) = on["comment_threads"]
            text = json.dumps(thread, ensure_ascii=False)
            ask = "staging permission" if case.lang == "en" else "ステージング"
            self.assertIn(ask, text, case.id)
            self.assertIn("1 resolved", on["comment_threads_note"], case.id)
            self.assertNotIn("Typo", json.dumps(on))
            self.assertNotIn("誤字", json.dumps(on, ensure_ascii=False))

    def test_the_title_matches_what_the_title_only_cases_expect(self):
        for case in self.cases:
            if "title-only" not in case.id:
                continue
            raw, note_id = self.prepared(case)
            out = REGISTRY["fetch_note"].run(
                {"note_type": "task", "note_id": note_id},
                ToolContext(team_id=raw["team_id"], user_id=raw["user_id"]),
            )
            wanted = case.raw["expect"]["answer_contains_any"]
            self.assertTrue(any(w.lower() in out["title"].lower() for w in wanted), case.id)

    def test_the_task_is_reachable_but_with_the_anchors_flag_off_it_does_not_lead_to_the_note(self):
        """Why the id has to be in the query: nothing the model can call before it names the note."""
        raw, note_id = self.prepared(self.cases[0])
        ctx = ToolContext(team_id=raw["team_id"], user_id=raw["user_id"])
        task = TaskMaster.objects.get(title=seeds.NOTE_TITLES["en"]["task"])
        for tool, args in (
            ("list_tasks", {}),
            ("fetch_task", {"task_id": task.task_id}),
        ):
            self.assertNotIn(f"task:{note_id}", json.dumps(REGISTRY[tool].run(args, ctx)), tool)

    def test_a_placeholder_without_a_note_is_a_harness_error(self):
        case = harness.GroupCase(
            id="x",
            kind="behaviour",
            lang="en",
            tags=frozenset({"eval-group"}),
            fixture="sequence",
            raw={
                "id": "x",
                "query": "note {note_id}?",
                "fixture": "sequence",
                "viewer": "b",
                "expect": {},
            },
            source="x.yaml",
        )
        with self.assertRaises(harness.HarnessError):
            case_runner.prepare_case(case)

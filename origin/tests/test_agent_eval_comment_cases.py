"""The grouped-eval pieces of MIP-1562 (arm `parity`, case files `t1562-*.yaml`). No model runs.

The paid session measures the parity batch in ONE run, so what has to be right before it is the
files: the case names the viewer and fixture the sequence scenario has, every case exists in both
languages, the positives need the arm (they cannot pass without the parameters being declared) and
the negatives run on arm0 too (the baseline), and each case's own `pending_write` spec passes on
the approval card the real card code builds for a proposal it should accept, and fails on one it
should not.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals.group import content_checks, harness, sequence
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT

from .test_base import BaseAPITestCase

FLAG = "AGENT_PARITY_TOOLS"
POSITIVES = (
    "reaction-add",
    "reaction-remove",
    "flag-set",
    "flag-done",
    "flag-unflag",
    "todo-comment",
    "todo-task",
)
NEGATIVES = ("edit-own", "reply-stays-a-comment", "todo-plain", "todo-reminder-plain")
UPDATE_ARGS = {"reaction", "remove_reaction", "flag"}
TODO_ARGS = {"task_id", "comment_id"}


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1562" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_positive_and_negative_is_there_in_both_languages(self):
        for slug in (*POSITIVES, *NEGATIVES):
            for lang in ("en", "ja"):
                self.assertIn(f"t1562.{slug}.{lang}", self.by_id)
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

    def test_every_viewer_is_one_the_scenario_has(self):
        for c in self.cases:
            self.assertIn(c.raw["viewer"], sequence.VIEWERS, c.id)
            self.assertEqual(c.raw["fixture"], "sequence", c.id)

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
            self.assertIn("pending_write", expect, c.id)
            self.assertIn("tool", expect["pending_write"], c.id)

    def test_every_check_key_is_one_the_harness_knows_and_every_pattern_compiles(self):
        known = {"tool", "args_present", "args_absent", "args_match", "task_args_match"}
        for c in self.cases:
            spec = c.raw["expect"]["pending_write"]
            self.assertFalse(set(spec) - known, c.id)
            for name, pattern in (spec.get("args_match") or {}).items():
                re.compile(pattern)
                self.assertTrue(name, c.id)

    def test_the_negatives_forbid_every_parameter_the_positives_add(self):
        edit = self.by_id["t1562.edit-own.en"].raw["expect"]["pending_write"]
        self.assertEqual(set(edit["args_absent"]), UPDATE_ARGS)
        for slug in ("todo-plain", "todo-reminder-plain"):
            spec = self.by_id[f"t1562.{slug}.en"].raw["expect"]["pending_write"]
            self.assertEqual(set(spec["args_absent"]), TODO_ARGS, slug)

    def test_the_cases_quote_comments_the_scenario_really_has(self):
        """A case that quotes a comment the story does not have would send the model looking for
        one that is not there. What the user TYPES (a title, an edit) is listed, not guessed."""
        from origin.services.dev_fixtures import sequence_scenario  # noqa: PLC0415

        story = set(sequence_scenario.TEXT.values()) | set(sequence.JA_TEXTS.values())
        typed = {
            "Fix the export that fails (Y)",
            "Follow up on the missing permission",
            "Review the export fix",
            "Buy more coffee for the office",
            "Thanks, please also check the CSV export and the PDF export",
            "権限不足の件をフォローする",
            "エクスポートの修正をレビューする",
            "オフィス用のコーヒーを買い足す",
            "ありがとうございます。CSVエクスポートとPDFエクスポートも確認してください",
        }
        for c in self.cases:
            quoted = re.findall(r'"([^"]+)"|「([^」]+)」', c.raw["query"])
            for text in (a or b for a, b in quoted):
                self.assertIn(text, story | typed, f"{c.id}: {text!r}")
        # ...and each comment a positive points at is one the story has.
        self.assertIn("Fixed and shipped, Y is solved", story)


class CardShapeTests(BaseAPITestCase):
    """`pending_write` judges the proposal AS THE APPROVAL CARD SHOWS IT. This builds the event
    arguments with the real card code, flag on, for a proposal each case should pass, and runs the
    case's own spec over them (and over one it must fail, so the check is not vacuous)."""

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
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="T", status="WIP"
        )
        channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.comment_id = ADD_COMMENT.run(
            {"task_id": self.task.task_id, "body_text": "Fixed and shipped"},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)),
        )["comment_id"]
        self.by_id = {c.id: c for c in cases()}

    def verdict(self, case_id, tool, proposal):
        # A comment call names its task and comment; a to-do proposal says only what it sends.
        base = (
            {}
            if tool == "create_todo_item"
            else {
                "task_id": self.task.task_id,
                "comment_id": self.comment_id,
            }
        )
        card = controller._friendly_arguments({**base, **proposal}, tool, ctx=self.ctx)
        spec = self.by_id[case_id].raw["expect"]["pending_write"]
        out = content_checks.check_pending(spec, [{"tool_name": tool, "arguments": card}])
        return {c["name"]: c["ok"] for c in out}, card

    def passes(self, case_id, tool, proposal):
        checks, card = self.verdict(case_id, tool, proposal)
        self.assertTrue(all(checks.values()), (case_id, checks, card))
        return card

    def fails(self, case_id, tool, proposal):
        checks, card = self.verdict(case_id, tool, proposal)
        self.assertFalse(all(checks.values()), (case_id, checks, card))

    def test_the_reaction_cases(self):
        for lang in ("en", "ja"):
            card = self.passes(f"t1562.reaction-add.{lang}", "update_comment", {"reaction": "👍"})
            self.assertEqual(card["comment"]["author"], "otheruser")
            self.passes(
                f"t1562.reaction-remove.{lang}",
                "update_comment",
                {"reaction": "🎉", "remove_reaction": True},
            )
        self.fails("t1562.reaction-add.en", "update_comment", {"body_text": "👍"})
        self.fails("t1562.reaction-add.en", "update_comment", {"reaction": "👎"})
        self.fails("t1562.reaction-remove.en", "update_comment", {"reaction": "🎉"})

    def test_the_flag_cases(self):
        for slug, value in (("flag-set", "flag"), ("flag-done", "done"), ("flag-unflag", "unflag")):
            for lang in ("en", "ja"):
                self.passes(f"t1562.{slug}.{lang}", "update_comment", {"flag": value})
        self.fails("t1562.flag-set.en", "update_comment", {"flag": "done"})
        self.fails("t1562.flag-set.en", "update_comment", {"flag": "flag", "reaction": "👍"})

    def test_an_edit_stays_an_edit(self):
        for lang in ("en", "ja"):
            self.passes(f"t1562.edit-own.{lang}", "update_comment", {"body_text": "new"})
        self.fails("t1562.edit-own.en", "update_comment", {"body_text": "new", "reaction": "👍"})
        self.fails("t1562.edit-own.en", "update_comment", {"reaction": "👍"})

    def test_a_reply_is_an_add_comment(self):
        for lang in ("en", "ja"):
            self.passes(
                f"t1562.reply-stays-a-comment.{lang}", "add_comment", {"body_text": "thanks"}
            )
        self.fails("t1562.reply-stays-a-comment.en", "update_comment", {"reaction": "👍"})

    def test_the_to_do_cases(self):
        for lang in ("en", "ja"):
            self.passes(
                f"t1562.todo-comment.{lang}",
                "create_todo_item",
                {"title": "T", "task_id": str(self.task.task_id), "comment_id": self.comment_id},
            )
            self.passes(
                f"t1562.todo-task.{lang}",
                "create_todo_item",
                {"title": "T", "task_id": str(self.task.task_id)},
            )
            for slug in ("todo-plain", "todo-reminder-plain"):
                self.passes(
                    f"t1562.{slug}.{lang}",
                    "create_todo_item",
                    {"title": "T", "remind_at": "2027-01-01"},
                )
        self.fails("t1562.todo-comment.en", "create_todo_item", {"title": "T", "task_id": "1"})
        self.fails(
            "t1562.todo-task.en",
            "create_todo_item",
            {"title": "T", "task_id": "1", "comment_id": 1},
        )
        self.fails("t1562.todo-plain.en", "create_todo_item", {"title": "T", "task_id": "1"})

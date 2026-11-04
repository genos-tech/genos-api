"""The map of what can be undone (`agent/undo.py`): `UNDO_SUPPORT`, held to the registry.

Which approval-gated writes can be undone is one reviewed table, in the style of
`TOOL_CATEGORY` and `EDITABLE_FIELDS`, enforced both ways: a new write tool cannot
merge without saying whether it can be undone, and a name that is not a write tool
cannot sit in the table. The rest pins what the approval card is told before the
person approves (`proposal`), and how a stored undo reads.

No LLM, no network. The proposals that look a task up (`update_task`) need a database.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import approval_edits, undo, undo_apply
from origin.search_engine.agent.tools import REGISTRY
from origin.views.public.mcp.tools import CATALOG

from .test_base import BaseAPITestCase


def _approval_tools() -> set[str]:
    return {name for name, tool in REGISTRY.items() if tool.requires_approval}


class MapCompletenessTests(SimpleTestCase):
    def test_every_approval_tool_declares_its_undo_and_nothing_else_is_listed(self):
        gated = _approval_tools()
        missing = gated - set(undo.UNDO_SUPPORT)
        self.assertFalse(
            missing,
            f"approval-gated tools with no entry in UNDO_SUPPORT: {sorted(missing)}. Say whether "
            "a person can undo one: 'full', 'partial' or 'none' "
            "(origin/search_engine/agent/undo.py).",
        )
        stale = set(undo.UNDO_SUPPORT) - gated
        self.assertFalse(
            stale,
            f"UNDO_SUPPORT names tools that are not approval-gated tools in the registry: "
            f"{sorted(stale)}",
        )

    def test_every_level_is_a_known_one(self):
        for name, level in undo.UNDO_SUPPORT.items():
            with self.subTest(tool=name):
                self.assertIn(level, undo.LEVELS)

    def test_a_tool_that_is_not_full_says_why_and_a_full_one_does_not(self):
        for name, level in undo.UNDO_SUPPORT.items():
            with self.subTest(tool=name):
                if level == undo.FULL:
                    self.assertNotIn(name, undo.UNDO_REASONS)
                    continue
                code, line = undo.UNDO_REASONS[name]
                self.assertIn(code, undo.REASON_CODES)
                self.assertTrue(line.strip())
        self.assertFalse(
            set(undo.UNDO_REASONS) - set(undo.UNDO_SUPPORT), "a reason for a tool not in the map"
        )

    def test_the_tools_filed_as_telling_people_are_the_reviewed_ones(self):
        """`notified_people` is a claim that a person is told, and what the card says
        before the person approves goes by it. A tool joins this set only with a test in
        `test_agent_undo_reasons` that runs it and sees the notice."""
        told = {
            name for name, (code, _) in undo.UNDO_REASONS.items() if code == undo.NOTIFIED_PEOPLE
        }
        self.assertEqual(
            told, {"approve_join_request", "decline_join_request", "create_calendar_event"}
        )

    def test_the_tools_v1_covers_are_the_reviewed_ones(self):
        covered = {name for name, level in undo.UNDO_SUPPORT.items() if level != undo.NONE}
        self.assertEqual(
            covered,
            {
                "create_task",
                "update_task",
                "restore_task_body",
                "add_comment",
                "create_note",
                "update_note",
                "restore_note_version",
            },
        )
        self.assertEqual(
            {name for name, level in undo.UNDO_SUPPORT.items() if level == undo.PARTIAL},
            {"update_task", "update_note"},
        )

    def test_every_mcp_destructive_tool_is_none(self):
        """The MCP rule is "a destructive tool needs a real undo before it belongs here":
        so a tool published as destructive is by definition one with no undo, and the
        card must never promise one."""
        destructive = {t.registry_name for t in CATALOG if t.destructive}
        self.assertTrue(destructive, "the MCP catalog lists no destructive tool: scan is stale")
        for name in sorted(destructive):
            with self.subTest(tool=name):
                self.assertEqual(undo.UNDO_SUPPORT[name], undo.NONE)

    def test_nothing_that_deletes_or_shares_is_undoable(self):
        for name in sorted(_approval_tools()):
            if name.startswith(("delete_", "approve_", "decline_")) or name in (
                "manage_note_sharing",
                "manage_project_members",
            ):
                with self.subTest(tool=name):
                    self.assertEqual(undo.UNDO_SUPPORT[name], undo.NONE)


class PartialToolsDecideEveryArgumentTests(SimpleTestCase):
    """A `partial` tool decides per call from its arguments, so every argument of its
    schema must be classified as one undo can put back or one it cannot: a new argument
    added to the tool is then a decision in this file, not a silent hole."""

    def test_update_task_classifies_its_whole_schema(self):
        declared = set(REGISTRY["update_task"].parameters_schema["properties"])
        classified = undo.UPDATE_TASK_RESTORABLE_ARGS | undo.UPDATE_TASK_UNRESTORABLE_ARGS
        self.assertEqual(declared, classified)
        self.assertFalse(undo.UPDATE_TASK_RESTORABLE_ARGS & undo.UPDATE_TASK_UNRESTORABLE_ARGS)

    def test_update_note_classifies_its_whole_schema(self):
        declared = set(REGISTRY["update_note"].parameters_schema["properties"])
        classified = undo.UPDATE_NOTE_RESTORABLE_ARGS | undo.UPDATE_NOTE_UNRESTORABLE_ARGS
        self.assertEqual(declared, classified)
        self.assertFalse(undo.UPDATE_NOTE_RESTORABLE_ARGS & undo.UPDATE_NOTE_UNRESTORABLE_ARGS)

    def test_the_restorable_columns_are_the_ones_the_tool_writes(self):
        written = {
            "title",
            "status",
            "priority",
            "effort_level",
            "due_date",
            "start_date",
            "tags",
        }
        self.assertEqual(set(undo.TASK_COLUMNS), written)

    def test_an_edit_before_approve_cannot_change_what_the_card_promised(self):
        """The card says "undoable" for the proposal as it stands, and the person may then
        edit values on it. An edit changes a value the model proposed and never adds a
        field, so no editable path may be one undo cannot restore: if it were, an edit
        could turn the promise false."""
        unrestorable = {
            "update_task": undo.UPDATE_TASK_UNRESTORABLE_ARGS,
            "update_note": undo.UPDATE_NOTE_UNRESTORABLE_ARGS,
        }
        for name, level in undo.UNDO_SUPPORT.items():
            if level == undo.NONE:
                continue
            for path in approval_edits.EDITABLE_FIELDS[name]:
                top = path.split("[")[0].split(".")[0]
                with self.subTest(tool=name, path=path):
                    self.assertNotIn(top, unrestorable.get(name, set()))


class ProposalTests(SimpleTestCase):
    def test_a_full_tool_is_undoable(self):
        for name in ("create_task", "add_comment", "create_note", "restore_task_body"):
            with self.subTest(tool=name):
                self.assertEqual(undo.proposal(name, {}), undo.Proposal(True, None))

    def test_a_tool_with_no_undo_says_why(self):
        self.assertEqual(
            undo.proposal("delete_project", {"project_id": 5}),
            undo.Proposal(False, "irreversible_delete"),
        )
        self.assertEqual(
            undo.proposal("approve_join_request", {}), undo.Proposal(False, "notified_people")
        )
        for name in ("manage_project_members", "assign_task", "manage_note_sharing"):
            self.assertEqual(undo.proposal(name, {}), undo.Proposal(False, "not_yet"), name)
        self.assertEqual(undo.proposal("update_comment", {}), undo.Proposal(False, "no_snapshot"))
        self.assertEqual(undo.proposal("update_tasks_bulk", {}), undo.Proposal(False, "not_yet"))

    def test_a_name_outside_the_map_is_not_undoable(self):
        self.assertFalse(undo.proposal("some_new_tool", {}).undoable)

    def test_update_note_with_a_folder_is_not_undoable_even_when_it_is_null(self):
        base = {"note_id": 3, "note_type": "personal", "title": "T"}
        self.assertTrue(undo.proposal("update_note", base).undoable)
        for folder in (7, None):
            with self.subTest(folder=folder):
                self.assertEqual(
                    undo.proposal("update_note", {**base, "folder_id": folder}),
                    undo.Proposal(False, "unsupported_fields"),
                )

    def test_a_note_argument_nobody_classified_fails_closed(self):
        self.assertEqual(
            undo.proposal("update_note", {"note_id": 3, "title": "T", "icon": "x"}),
            undo.Proposal(False, "unsupported_fields"),
        )


class UpdateTaskProposalTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="T", status="Open"
        )

    def test_the_fields_undo_restores_are_undoable(self):
        args = {
            "task_id": self.task.task_id,
            "title": "New",
            "status": "WIP",
            "priority": "High",
            "effort_level": "Low",
            "due_date": "2026-12-01",
            "start_date": "",
            "tags": [],
            "content_text": "body",
            "base_body_hash": "abc",
        }
        self.assertEqual(undo.proposal("update_task", args), undo.Proposal(True, None))

    def test_anything_it_cannot_put_back_makes_the_call_not_undoable(self):
        unrestorable = {
            "collaborators": [],
            "custom_fields": {"a": 1},
            "milestone_id": 4,
            "parent_task_id": 9,
            "project_id": 2,
        }
        for key, value in unrestorable.items():
            with self.subTest(argument=key):
                self.assertEqual(
                    undo.proposal("update_task", {"task_id": self.task.task_id, key: value}),
                    undo.Proposal(False, "unsupported_fields"),
                )

    def test_an_empty_value_of_an_unrestorable_argument_is_not_asked_for(self):
        """`None` is "omitted" to the tool, as it is to every other check."""
        args = {"task_id": self.task.task_id, "title": "x", "milestone_id": None}
        self.assertTrue(undo.proposal("update_task", args).undoable)

    def test_a_milestones_backing_task_is_not_undoable(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_milestone=True)
        self.assertEqual(
            undo.proposal("update_task", {"task_id": self.task.task_id, "title": "x"}),
            undo.Proposal(False, "unsupported_fields"),
        )

    def test_a_task_that_is_not_there_is_judged_on_its_arguments(self):
        self.assertTrue(undo.proposal("update_task", {"task_id": 99999999, "title": "x"}).undoable)
        self.assertTrue(undo.proposal("update_task", {"task_id": "nope", "title": "x"}).undoable)


class StoredShapeTests(SimpleTestCase):
    def test_a_stored_undo_names_the_tool_and_when_it_was_written(self):
        now = timezone.now()
        body = undo.stored(
            "create_task",
            {"entity": {"kind": "task", "task_id": 4}, "image": {"task_id": 4}},
            now=now,
        )
        self.assertEqual(body["v"], undo.SCHEMA_VERSION)
        self.assertEqual(body["tool"], "create_task")
        self.assertEqual(body["captured_at"], now.isoformat())
        self.assertEqual(body["entity"], {"kind": "task", "task_id": 4})
        json.dumps(body)

    def test_nothing_is_stored_without_an_image(self):
        for payload in (None, {}, {"entity": {}}, {"image": "x"}, "x", []):
            with self.subTest(payload=payload):
                self.assertIsNone(undo.stored("create_task", payload))

    def test_a_pre_image_that_is_too_heavy_is_dropped(self):
        payload = {"entity": {}, "image": {"blob": "x" * undo.MAX_STORED_BYTES}}
        self.assertIsNone(undo.stored("create_task", payload))

    def test_the_window_is_seven_days_from_the_write(self):
        self.assertEqual(undo.UNDO_WINDOW, timedelta(days=7))
        written = timezone.now()
        body = undo.stored("create_task", {"image": {"a": 1}}, now=written)
        self.assertEqual(undo.available_until(body), written + timedelta(days=7))
        self.assertFalse(undo.is_expired(body, now=written + timedelta(days=7, seconds=-1)))
        self.assertTrue(undo.is_expired(body, now=written + timedelta(days=7)))

    def test_a_stamp_nobody_can_read_is_expired(self):
        self.assertTrue(undo.is_expired({"captured_at": "yesterday-ish"}))
        self.assertTrue(undo.is_expired(None))

    def test_the_step_info_is_what_the_browser_gets(self):
        written = timezone.now()
        body = undo.stored("add_comment", {"image": {"a": 1}}, now=written)
        step = dataclasses.make_dataclass(
            "Step", ["step_id", "step_index", "tool_name", "undo_json", "undone_at"]
        )(11, 2, "add_comment", body, None)
        self.assertEqual(
            undo.step_info(step),
            {
                "step_id": 11,
                "step": 2,
                "tool_name": "add_comment",
                "available_until": (written + timedelta(days=7)).isoformat(),
                "undone_at": None,
            },
        )
        self.assertEqual(
            undo.event_info(step),
            {"step_id": 11, "available_until": (written + timedelta(days=7)).isoformat()},
        )
        step.undone_at = written
        self.assertEqual(undo.step_info(step)["undone_at"], written.isoformat())

    def test_a_step_with_no_undo_has_no_info(self):
        step = dataclasses.make_dataclass(
            "Step", ["step_id", "step_index", "tool_name", "undo_json", "undone_at"]
        )(1, 0, "x", None, None)
        self.assertIsNone(undo.step_info(step))
        self.assertIsNone(undo.event_info(step))


class HandlersMatchTheMapTests(SimpleTestCase):
    def test_every_tool_that_can_be_undone_has_a_handler_and_only_those(self):
        can_be_undone = {name for name, level in undo.UNDO_SUPPORT.items() if level != undo.NONE}
        missing = can_be_undone - set(undo_apply.HANDLERS)
        self.assertFalse(missing, f"the map promises an undo no handler does: {sorted(missing)}")
        extra = set(undo_apply.HANDLERS) - can_be_undone
        self.assertFalse(
            extra, f"a handler for a tool the map says cannot be undone: {sorted(extra)}"
        )

    def test_every_refusal_code_has_a_status(self):
        self.assertEqual(
            undo_apply.STATUS,
            {
                "expired": 410,
                "changed_since": 409,
                "already_undone": 409,
                "not_undoable": 409,
                "permission_lost": 403,
            },
        )

"""What an approved note write tells the browser (MIP-950, MIP-951).

The stream's `tool_call_result` carries a compact `note` ref for note writes, so the
browser can refresh what it caches. `create_note` and `update_note` had one; a delete
and a restore did not, so the browser kept a tab and a local copy of a note that was
gone, and an open editor kept the old title of a note that had been restored.

Driven through `resume_agent`, the leg that runs an approved write and emits the event,
with the tool swapped for one that returns a given result: what is pinned here is what
the controller does with a result, not what each tool computes (the tools' own tests
pin that).
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER, resume_agent
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.models import AgentRun, AgentStep


class ApprovedWriteEvent(TestCase):
    def approve(self, tool_name, result):
        """The `tool_call_result` event an approved call of `tool_name` emits."""
        run = AgentRun.objects.create(
            team_id="t1",
            user_id="u1",
            query="do it",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name=tool_name,
            arguments_json={},
            summary=PENDING_APPROVAL_MARKER,
        )
        fake = SimpleNamespace(
            name=tool_name,
            requires_approval=True,
            run=mock.Mock(return_value={**result, "__summary__": "done"}),
        )
        events = []
        with (
            mock.patch.object(controller, "_drive_loop", return_value=None),
            mock.patch.dict(REGISTRY, {tool_name: fake}),
        ):
            resume_agent(run, "approve", ToolContext(team_id="t1", user_id="u1"), events.append)
        (event,) = [e for e in events if e["type"] == "tool_call_result"]
        return event


class NoteRefs(ApprovedWriteEvent):
    def test_a_delete_names_the_note_and_says_it_is_gone(self):
        event = self.approve(
            "delete_note",
            {"note_type": "personal", "note_id": 7, "title": "Plan", "deleted": True},
        )
        self.assertEqual(
            event["note"],
            {"note_id": 7, "note_type": "personal", "title": "Plan", "deleted": True},
        )
        self.assertNotIn("changed_fields", event["note"])

    def test_a_restore_says_the_title_and_the_body_changed(self):
        event = self.approve(
            "restore_note_version",
            {
                "note_type": "task",
                "note_id": 9,
                "title": "Plan v1",
                "changed_fields": ["title", "body"],
                "live_editor": "sent",
                "restored_from_version_no": 1,
            },
        )
        self.assertEqual(
            event["note"],
            {
                "note_id": 9,
                "note_type": "task",
                "title": "Plan v1",
                "changed_fields": ["title", "body"],
            },
        )
        self.assertNotIn("deleted", event["note"])

    def test_an_update_and_a_create_are_as_before(self):
        updated = self.approve(
            "update_note",
            {"note_type": "personal", "note_id": 3, "title": "T", "changed_fields": ["body"]},
        )
        self.assertEqual(
            updated["note"],
            {"note_id": 3, "note_type": "personal", "title": "T", "changed_fields": ["body"]},
        )
        created = self.approve("create_note", {"note_type": "personal", "note_id": 4, "title": "N"})
        self.assertEqual(created["note"], {"note_id": 4, "note_type": "personal", "title": "N"})

    def test_a_result_with_no_note_id_carries_no_ref(self):
        event = self.approve("delete_note", {"note_type": "personal", "deleted": True})
        self.assertNotIn("note", event)

    def test_other_tools_get_no_note_ref_even_with_the_same_keys(self):
        event = self.approve(
            "delete_milestone",
            {"note_id": 7, "note_type": "personal", "title": "x", "deleted": True},
        )
        self.assertNotIn("note", event)
        self.assertNotIn("note_folder", event)


class NoteFolderRef(ApprovedWriteEvent):
    RESULT = {
        "folder_id": 5,
        "name": "Drafts",
        "deleted_folders": 2,
        "deleted_notes": 3,
        "deleted_note_ids": [11, 12, 13],
        "deleted_folder_ids": [5, 6],
        "ids_truncated": False,
    }

    def test_a_folder_delete_names_every_folder_and_note_that_went(self):
        event = self.approve("delete_note_folder", self.RESULT)
        self.assertEqual(
            event["note_folder"],
            {
                "folder_id": 5,
                "deleted_folder_ids": [5, 6],
                "deleted_note_ids": [11, 12, 13],
                "ids_truncated": False,
            },
        )
        self.assertNotIn("note", event)

    def test_the_ref_leaves_out_the_name_and_the_counts(self):
        # The browser has the name already and counts nobody needs; fewer fields is less
        # to keep in step with the tool.
        keys = set(self.approve("delete_note_folder", self.RESULT)["note_folder"])
        self.assertTrue(keys.isdisjoint({"name", "deleted_folders", "deleted_notes"}))

    def test_a_capped_list_says_so(self):
        event = self.approve(
            "delete_note_folder", {**self.RESULT, "ids_truncated": True, "deleted_note_ids": [1]}
        )
        self.assertIs(event["note_folder"]["ids_truncated"], True)

    def test_another_tool_gets_no_folder_ref(self):
        event = self.approve(
            "delete_note", {"note_id": 1, "note_type": "personal", "deleted": True}
        )
        self.assertNotIn("note_folder", event)

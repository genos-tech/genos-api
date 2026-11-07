"""Over MCP, milestones are managed by the names people use (MIP-879).

`manage_milestone` and `delete_milestone` are published with the rest of the
writes. The tool-level rules are in `test_agent_tool_manage_milestone` and
`test_agent_tool_delete_milestone`; this is what the endpoint adds: who sees
which tool (the delete needs the key's `allow_destructive`), a milestone named
by its display id, the arguments as published, and the refusal of an argument
the tool does not take.
"""

from __future__ import annotations

import json

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.task.milestone_models import MilestoneFolder, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_service import create_milestone
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools


class McpMilestoneBase(McpBase):
    def setUp(self):
        super().setUp()
        self.milestone = create_milestone(
            self.project, reporter_id=self.user.id, title="Launch the redesign"
        )
        self.display = self.milestone.task.display_id  # e.g. WRD-8
        self.folder = MilestoneFolder.objects.create(
            team=self.team, project=self.project, name="Launch"
        )

    def _text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def _structured(self, name, arguments):
        result = self._result(self._call(name, arguments))
        self.assertFalse(result.get("isError"), result["content"][0]["text"])
        return result["structuredContent"]


class VisibilityTests(McpMilestoneBase):
    def test_a_write_key_sees_the_edit_tool_but_not_the_delete(self):
        self._key(scope=SCOPE_WRITE)
        listed = self._listed()
        self.assertIn("manage_milestone", listed)
        self.assertNotIn("delete_milestone", listed)

    def test_a_key_opted_into_destructive_tools_sees_both(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self._listed()
        self.assertIn("manage_milestone", listed)
        self.assertIn("delete_milestone", listed)

    def test_a_read_key_sees_neither(self):
        self._key(scope=SCOPE_READ, allow_destructive=True)
        listed = self._listed()
        self.assertNotIn("manage_milestone", listed)
        self.assertNotIn("delete_milestone", listed)

    def test_the_annotations_say_which_call_is_the_delete(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self._listed()
        self.assertIs(listed["delete_milestone"]["annotations"]["destructiveHint"], True)
        # Announced as destructive (it can overwrite a milestone), though not gated.
        self.assertIs(listed["manage_milestone"]["annotations"]["destructiveHint"], True)

    def test_the_delete_is_its_own_tool_so_the_opt_in_covers_nothing_else(self):
        """A key without the opt-in can still create and edit milestones."""
        by_name = {t.name: t for t in tools.CATALOG}
        self.assertTrue(by_name["delete_milestone"].destructive)
        self.assertFalse(by_name["manage_milestone"].destructive)
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "manage_milestone",
            {"action": "create", "project_id": self.project.project_id, "title": "Made"},
        )
        self.assertFalse(is_error, text)


class DeleteOverMcpTests(McpMilestoneBase):
    def test_without_the_opt_in_the_delete_is_refused_and_nothing_changes(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text("delete_milestone", {"milestone_id": self.display})
        self.assertTrue(is_error)
        self.assertIn("destructive", text)
        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_deleted)

    def test_an_opted_in_key_deletes_by_display_id_and_keeps_the_tasks(self):
        kid = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="kid",
            status="Open",
            milestone=self.milestone,
            parent_task_id=self.milestone.task_id,
        )
        self._key(scope=SCOPE_WRITE, allow_destructive=True)

        text, is_error = self._text("delete_milestone", {"milestone_id": self.display})

        self.assertFalse(is_error, text)
        self.assertIn("Launch the redesign", text)
        self.milestone.refresh_from_db()
        self.assertTrue(self.milestone.is_deleted)
        kid.refresh_from_db()
        self.assertFalse(kid.is_deleted)
        self.assertIsNone(kid.milestone_id)

    def test_a_numeric_id_works_and_a_task_that_is_not_a_milestone_does_not(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text, is_error = self._text("delete_milestone", {"milestone_id": "WRD-7"})
        self.assertTrue(is_error)
        self.assertIn("not a milestone", text)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_deleted)

        text, is_error = self._text(
            "delete_milestone", {"milestone_id": str(self.milestone.milestone_id)}
        )
        self.assertFalse(is_error, text)

    def test_the_description_says_it_cannot_be_undone(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text = self._listed()["delete_milestone"]["description"]
        self.assertIn("not reversible", text)
        self.assertIn("tasks are kept", text)


class ManageOverMcpTests(McpMilestoneBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)

    def test_a_milestone_is_edited_by_its_display_id(self):
        text, is_error = self._text(
            "manage_milestone",
            {
                "action": "update",
                "milestone_id": self.display,
                "status": "WIP",
                "due_date": "2026-12-01",
                "milestone_folder_id": str(self.folder.folder_id),
                "assignee_ids": [str(self.user2.id)],
            },
        )
        self.assertFalse(is_error, text)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.status, "WIP")
        self.assertEqual(self.milestone.folder_id, self.folder.folder_id)
        self.assertEqual(
            list(self.milestone.milestone_assignees.values_list("user_id", flat=True)),
            [self.user2.id],
        )
        self.assertIn(self.display, text)

    def test_a_milestone_is_created_and_a_folder_made_and_renamed(self):
        made = self._structured(
            "manage_milestone",
            {"action": "create", "project_id": self.project.project_id, "title": "Over MCP"},
        )
        self.assertTrue(MilestoneMaster.objects.filter(milestone_id=made["milestone_id"]).exists())

        folder = self._structured(
            "manage_milestone",
            {"action": "create_folder", "project_id": self.project.project_id, "folder_name": "Q4"},
        )
        renamed = self._structured(
            "manage_milestone",
            {
                "action": "rename_folder",
                "milestone_folder_id": folder["folder_id"],
                "folder_name": "Q4 2026",
            },
        )
        self.assertEqual(renamed["name"], "Q4 2026")
        gone = self._structured(
            "manage_milestone",
            {"action": "delete_folder", "milestone_folder_id": folder["folder_id"]},
        )
        self.assertTrue(gone["deleted"])
        self.assertFalse(MilestoneFolder.objects.filter(folder_id=folder["folder_id"]).exists())

    def test_the_folder_ids_come_from_list_milestones(self):
        listed = self._structured("list_milestones", {"project_id": self.project.project_id})
        self.assertEqual([f["name"] for f in listed["folders"]], ["Launch"])
        folder_id = listed["folders"][0]["folder_id"]

        self._structured(
            "manage_milestone",
            {"action": "update", "milestone_id": self.display, "milestone_folder_id": folder_id},
        )

        listed = self._structured("list_milestones", {"project_id": self.project.project_id})
        row = next(
            r for r in listed["milestones"] if r["milestone_id"] == self.milestone.milestone_id
        )
        self.assertEqual(row["folder_id"], folder_id)

    def test_a_typo_in_an_argument_is_refused_with_a_suggestion_and_changes_nothing(self):
        text, is_error = self._text(
            "manage_milestone",
            {
                "action": "update",
                "milestone_id": self.display,
                "folder_id": self.folder.folder_id,
                "status": "WIP",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("did you mean `milestone_folder_id`", text)
        self.milestone.refresh_from_db()
        self.assertIsNone(self.milestone.folder_id)
        self.assertEqual(self.milestone.status, "Open")

    def test_the_update_tool_still_refuses_to_delete_a_milestone(self):
        # With the delete opt-in the call reaches the tool, which names `delete_milestone`.
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text, is_error = self._text("update_task", {"task_id": self.display, "status": "Deleted"})
        self.assertTrue(is_error)
        self.assertIn("can't delete one", text)
        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_deleted)

    def test_without_the_delete_opt_in_it_is_held_before_the_tool_is_reached(self):
        text, is_error = self._text("update_task", {"task_id": self.display, "status": "Deleted"})
        self.assertTrue(is_error)
        self.assertIn("destructive tools", text)
        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_deleted)

    def test_a_refusal_comes_back_as_a_tool_error_not_a_protocol_error(self):
        text, is_error = self._text(
            "manage_milestone",
            {"action": "update", "milestone_id": self.display, "priority": "Urgent"},
        )
        self.assertTrue(is_error)
        self.assertIn("`priority` must be one of", text)

    def test_the_arguments_are_published(self):
        props = self._listed()["manage_milestone"]["inputSchema"]["properties"]
        self.assertEqual(props["milestone_id"]["type"], "string")
        self.assertIn("display id", props["milestone_id"]["description"])
        self.assertEqual(props["action"]["enum"][0], "create")
        self.assertIn("milestone_folder_id", props)
        self.assertNotIn("folder_id", props)

    def test_the_sprint_argument_does_not_send_the_caller_to_an_unpublished_tool(self):
        listed = self._listed()
        description = listed["manage_milestone"]["inputSchema"]["properties"]["sprint_id"][
            "description"
        ]
        if "list_sprints" not in listed:
            self.assertNotIn("list_sprints", description)
        self.assertIn("list_milestones", description)

    def test_nothing_the_catalog_says_names_a_tool_this_surface_does_not_publish(self):
        published = {t.name for t in tools.CATALOG}
        for name in ("manage_milestone", "delete_milestone"):
            blob = json.dumps(tools.describe(tools.BY_NAME[name]))
            for other in ("create_task_plan", "list_project_tags", "get_team_members"):
                if other in blob:
                    self.assertIn(other, published, f"{name} mentions unpublished {other}")

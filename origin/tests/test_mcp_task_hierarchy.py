"""Over MCP, a task's parent and project are changed by the names people use (MIP-899).

`update_task` takes `parent_task_id` (a display id, a URL or a number, and
`''` for top-level) and `project_id`, and `create_task` takes
`parent_task_id`. The tool-level rules are in `test_agent_tool_task_reparent`;
this is what the endpoint adds: resolving the reference, letting a blank
through where a blank means something, and publishing the arguments.
"""

from __future__ import annotations

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_mcp_endpoint import McpBase


class McpHierarchyBase(McpBase):
    def setUp(self):
        super().setUp()
        self.parent = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Parent",
            status="Open",
            project_task_number=8,
        )
        self.other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.other, attendee=self.user)
        self._key(scope=SCOPE_WRITE)

    def _text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class TestUpdateTaskHierarchyOverMcp(McpHierarchyBase):
    def test_a_parent_is_named_by_display_id_and_cleared_with_an_empty_string(self):
        text, is_error = self._text("update_task", {"task_id": "WRD-7", "parent_task_id": "WRD-8"})
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.parent_task_id, self.parent.task_id)
        self.assertEqual(self.task.root_task_id, self.parent.task_id)

        text, is_error = self._text("update_task", {"task_id": "WRD-7", "parent_task_id": ""})
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.parent_task_id)
        self.assertEqual(self.task.root_task_id, self.task.task_id)

    def test_a_parent_can_be_named_by_url(self):
        url = (
            f"https://app.genosai.dev/workspace/tasks/project/{self.project.project_id}"
            f"/task/{self.parent.task_id}"
        )
        text, is_error = self._text("update_task", {"task_id": "WRD-7", "parent_task_id": url})
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.parent_task_id, self.parent.task_id)

    def test_a_parent_that_does_not_exist_says_so(self):
        text, is_error = self._text(
            "update_task", {"task_id": "WRD-7", "parent_task_id": "WRD-999"}
        )
        self.assertTrue(is_error)
        self.assertIn("WRD-999", text)

    def test_a_project_move_reports_the_new_display_id(self):
        text, is_error = self._text(
            "update_task", {"task_id": "WRD-7", "project_id": self.other.project_id}
        )
        self.assertFalse(is_error, text)
        self.assertIn("OTH-", text)
        self.assertIn("was WRD-7", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.other.project_id)

    def test_a_blank_is_still_refused_where_it_means_nothing(self):
        """Only `parent_task_id` on update_task treats '' as a value; a blank
        task reference anywhere else is an error, as before."""
        text, is_error = self._text("update_task", {"task_id": ""})
        self.assertTrue(is_error)

    def test_the_arguments_are_published(self):
        tools = self._listed()
        update = tools["update_task"]["inputSchema"]["properties"]
        create = tools["create_task"]["inputSchema"]["properties"]

        self.assertEqual(update["parent_task_id"]["type"], "string")
        self.assertIn("display id", update["parent_task_id"]["description"])
        self.assertIn("top-level", update["parent_task_id"]["description"])
        self.assertEqual(update["project_id"]["type"], "integer")
        self.assertEqual(create["parent_task_id"]["type"], "string")
        self.assertIn("display id", create["parent_task_id"]["description"])


class TestCreateTaskHierarchyOverMcp(McpHierarchyBase):
    def test_a_new_task_is_filed_under_a_parent_by_display_id(self):
        text, is_error = self._text(
            "create_task",
            {
                "title": "A sub-task",
                "project_id": self.project.project_id,
                "parent_task_id": "WRD-8",
            },
        )
        self.assertFalse(is_error, text)
        made = TaskMaster.objects.get(title="A sub-task")
        self.assertEqual(made.parent_task_id, self.parent.task_id)
        self.assertEqual(made.root_task_id, self.parent.task_id)
        self.assertIn("under WRD-8", text)

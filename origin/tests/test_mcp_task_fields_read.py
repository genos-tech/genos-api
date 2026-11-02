"""Over MCP, a task's fields are read and filtered by the names people use (MIP-877).

The tool-level rules are in `test_agent_tool_task_read_fields`. This is what
the endpoint adds: the fences come off and the untrusted-content notice leads
the answer, a parent task is named by display id, and `list_sprints` /
`get_sprint_summary` are published, so the sprint id `list_tasks` takes can be
found.
"""

from __future__ import annotations

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import (
    ProjectCustomField,
    ProjectMaster,
    ProjectSavedTaskFilter,
)
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools


class McpReadFieldsBase(McpBase):
    def setUp(self):
        super().setUp()
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 1",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
            status="active",
        )
        self.customer = ProjectCustomField.objects.create(
            team=self.team, project=self.project, field_name="Customer", field_type="text"
        )
        self._key(scope=SCOPE_READ)

    def _payload(self, name, arguments):
        result = self._result(self._call(name, arguments))
        self.assertFalse(result.get("isError"), result)
        return result

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class GetTaskFieldsOverMcpTests(McpReadFieldsBase):
    def test_the_new_fields_come_back_by_name_without_fences(self):
        self.task.sprint = self.sprint
        self.task.start_date = "2026-11-03"
        self.task.custom_field_values = {str(self.customer.field_id): "Acme"}
        self.task.save()
        self.task.collaborators.set([self.user2])

        result = self._payload("get_task", {"task_id": "WRD-7"})
        out = result["structuredContent"]

        self.assertEqual(out["start_date"], "2026-11-03")
        self.assertEqual(out["sprint"], {"id": self.sprint.sprint_id, "name": "Sprint 1"})
        self.assertEqual(
            out["collaborators"], [{"user_id": str(self.user2.id), "username": "otheruser"}]
        )
        self.assertEqual(
            out["custom_fields"], [{"name": "Customer", "type": "text", "value": "Acme"}]
        )
        self.assertNotIn("workspace_content", result["content"][0]["text"])
        self.assertTrue(result["content"][0]["text"].startswith(tools.UNTRUSTED_NOTICE))

    def test_the_description_names_the_new_fields(self):
        description = self._listed()["get_task"]["description"]

        for word in ("start_date", "sprint", "collaborators", "links", "custom_fields"):
            self.assertIn(word, description)


class ListTasksFiltersOverMcpTests(McpReadFieldsBase):
    def test_a_parent_is_named_by_display_id(self):
        parent = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Parent",
            status="Open",
            project_task_number=8,
        )
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Child",
            status="Open",
            parent_task_id=parent.task_id,
        )

        out = self._payload("list_tasks", {"parent_task_id": "WRD-8"})["structuredContent"]

        self.assertEqual([t["title"] for t in out["tasks"]], ["Child"])

    def test_a_parent_that_does_not_exist_says_so(self):
        result = self._result(self._call("list_tasks", {"parent_task_id": "WRD-999"}))

        self.assertTrue(result["isError"])
        self.assertIn("WRD-999", result["content"][0]["text"])

    def test_the_new_filters_are_published(self):
        properties = self._listed()["list_tasks"]["inputSchema"]["properties"]

        for key in ("tag", "sprint_id", "parent_task_id", "due_from", "due_to", "unassigned"):
            self.assertIn(key, properties)
        self.assertEqual(properties["parent_task_id"]["type"], "string")
        self.assertIn("display id", properties["parent_task_id"]["description"])
        self.assertEqual(properties["sprint_id"]["type"], "integer")
        self.assertEqual(properties["unassigned"]["type"], "boolean")

    def test_a_filter_misspelt_is_refused_not_ignored(self):
        result = self._result(self._call("list_tasks", {"due_before": "2026-11-30"}))

        self.assertTrue(result["isError"])
        self.assertIn("due_before", result["content"][0]["text"])
        self.assertIn("due_to", result["content"][0]["text"])


class ProjectVocabularyOverMcpTests(McpReadFieldsBase):
    def test_custom_fields_and_saved_filters_come_with_the_tags(self):
        ProjectSavedTaskFilter.objects.create(
            team=self.team,
            project=self.project,
            filter_name="Open work",
            filters={"status": ["Open", "WIP"]},
        )

        out = self._payload("list_project_tags", {"project_id": self.project.project_id})[
            "structuredContent"
        ]

        self.assertEqual(out["custom_fields"], [{"field_name": "Customer", "type": "text"}])
        self.assertEqual(
            [(f["filter_name"], f["status"]) for f in out["saved_filters"]],
            [("Open work", ["Open", "WIP"])],
        )

    def test_the_description_says_so(self):
        description = self._listed()["list_project_tags"]["description"]

        self.assertIn("custom_fields", description)
        self.assertIn("saved_filters", description)

    def test_a_project_the_caller_is_not_in_is_refused(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )

        result = self._result(self._call("list_project_tags", {"project_id": other.project_id}))

        self.assertTrue(result["isError"])


class SprintReadsOverMcpTests(McpReadFieldsBase):
    def test_both_are_published_and_a_read_key_sees_them(self):
        listed = self._listed()

        self.assertIn("list_sprints", listed)
        self.assertIn("get_sprint_summary", listed)
        self.assertEqual(listed["get_sprint_summary"]["inputSchema"]["required"], ["sprint_id"])

    def test_neither_description_points_at_a_tool_that_is_not_published(self):
        published = {t.name for t in tools.CATALOG}
        for name in ("list_sprints", "get_sprint_summary"):
            description = self._listed()[name]["description"]
            for token in ("list_tasks", "list_sprints"):
                if token in description:
                    self.assertIn(token, published)

    def test_list_sprints_finds_the_sprint_and_list_tasks_filters_by_it(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(sprint=self.sprint)

        sprints = self._payload("list_sprints", {"project_id": self.project.project_id})[
            "structuredContent"
        ]["sprints"]
        self.assertEqual([s["name"] for s in sprints], ["Sprint 1"])
        self.assertEqual(sprints[0]["tasks_total"], 1)

        tasks = self._payload("list_tasks", {"sprint_id": sprints[0]["sprint_id"]})[
            "structuredContent"
        ]["tasks"]
        self.assertEqual([t["display_id"] for t in tasks], ["WRD-7"])

    def test_get_sprint_summary_answers_for_a_member(self):
        summary = self._payload("get_sprint_summary", {"sprint_id": self.sprint.sprint_id})[
            "structuredContent"
        ]

        self.assertEqual(summary["name"], "Sprint 1")
        self.assertEqual(summary["status"], "active")

    def test_a_sprint_in_a_project_the_caller_is_not_in_is_refused(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = Sprint.objects.create(
            team=self.team,
            project=other,
            name="Hidden",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )

        result = self._result(self._call("get_sprint_summary", {"sprint_id": hidden.sprint_id}))
        listing = self._payload("list_sprints", {})["structuredContent"]

        self.assertTrue(result["isError"])
        self.assertNotIn("Hidden", repr(listing))

    def test_a_sprint_that_is_missing_foreign_or_not_the_callers_reads_the_same(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        not_a_member = Sprint.objects.create(
            team=self.team,
            project=other,
            name="Hidden",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )
        foreign = Sprint.objects.create(
            team=TeamMaster.objects.create(
                team_name="B", team_email="b@example.com", owner=self.user2
            ),
            project=self.project,
            name="Foreign",
            sequence_number=2,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )

        for tool, key in (("get_sprint_summary", "sprint_id"), ("list_tasks", "sprint_id")):
            answers = set()
            for sprint_id in (999999, not_a_member.sprint_id, foreign.sprint_id):
                result = self._result(self._call(tool, {key: sprint_id}))
                self.assertTrue(result["isError"], (tool, sprint_id))
                answers.add(result["content"][0]["text"].replace(str(sprint_id), "N"))
            self.assertEqual(answers, {"Sprint N not found."}, tool)

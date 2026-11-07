"""Over MCP, a sprint can be created, changed and deleted (MIP-882).

There is no approval card on this surface, so the key's scope is the only
authorization. The endpoint adds: the delete needs the destructive opt-in and the
create / edit don't, a read key sees neither, and the ids the calls need
(`project_id`, `sprint_id`) come from published reads.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.task.sprint_models import Sprint
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools

NEW = {"manage_sprint", "delete_sprint"}


def day(offset):
    return (timezone.localdate() + timedelta(days=offset)).isoformat()


class McpSprintBase(McpBase):
    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def run_tool(self, name, arguments):
        result = self._result(self._call(name, arguments))
        return result, result["content"][0]["text"]

    def make(self, name="Sprint 1", start=-3, end=10):
        return Sprint.objects.create(
            team=self.team,
            project=self.project,
            name=name,
            sequence_number=Sprint.objects.count() + 1,
            start_date=day(start),
            end_date=day(end),
        )


class TestWhoSeesWhat(McpSprintBase):
    def test_a_write_key_sees_the_manage_tool_but_not_the_delete(self):
        self._key(scope=SCOPE_WRITE)
        self.assertEqual(NEW & set(self.listed()), {"manage_sprint"})

    def test_the_destructive_opt_in_adds_the_delete(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertEqual(NEW & set(self.listed()), NEW)
        self.assertIs(self.listed()["delete_sprint"]["annotations"]["destructiveHint"], True)
        self.assertIs(self.listed()["manage_sprint"]["annotations"]["destructiveHint"], False)

    def test_a_read_key_sees_neither(self):
        self._key(scope=SCOPE_READ)
        self.assertFalse(NEW & set(self.listed()))

    def test_the_catalog_points_at_the_reads_that_give_the_ids(self):
        self.assertIn("list_projects", tools.BY_NAME["manage_sprint"].description)
        self.assertIn("list_sprints", tools.BY_NAME["manage_sprint"].description)
        self.assertIn("list_sprints", tools.BY_NAME["delete_sprint"].description)
        self.assertIn("end_date", tools.BY_NAME["manage_sprint"].description)


class TestTheCallsWork(McpSprintBase):
    def test_a_sprint_is_created_and_then_seen_by_list_sprints(self):
        self._key(scope=SCOPE_WRITE)
        result, _ = self.run_tool(
            "manage_sprint",
            {
                "action": "create",
                "project_id": self.project.project_id,
                "name": "Polish Week",
                "start_date": day(60),
                "end_date": day(73),
            },
        )
        self.assertFalse(result.get("isError"), result)
        created = result["structuredContent"]["sprint_id"]
        listing, _ = self.run_tool("list_sprints", {"project_id": self.project.project_id})
        self.assertIn(created, [s["sprint_id"] for s in listing["structuredContent"]["sprints"]])

    def test_a_sprint_is_ended_early_by_its_end_date(self):
        self._key(scope=SCOPE_WRITE)
        sprint = self.make()
        result, _ = self.run_tool(
            "manage_sprint",
            {"action": "update", "sprint_id": sprint.sprint_id, "end_date": day(-1)},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["status"], "completed")

    def test_an_overlap_comes_back_as_an_error_and_changes_nothing(self):
        self._key(scope=SCOPE_WRITE)
        self.make()
        result, text = self.run_tool(
            "manage_sprint",
            {
                "action": "create",
                "project_id": self.project.project_id,
                "name": "clash",
                "start_date": day(0),
                "end_date": day(5),
            },
        )
        self.assertTrue(result.get("isError"), result)
        self.assertIn("overlaps", text)
        self.assertEqual(Sprint.objects.count(), 1)

    def test_the_delete_is_refused_without_the_opt_in(self):
        self._key(scope=SCOPE_WRITE)
        sprint = self.make()
        result, text = self.run_tool("delete_sprint", {"sprint_id": sprint.sprint_id})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("destructive", text.lower())
        sprint.refresh_from_db()
        self.assertFalse(sprint.is_deleted)

    def test_the_delete_works_with_the_opt_in(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        sprint = self.make()
        result, _ = self.run_tool("delete_sprint", {"sprint_id": sprint.sprint_id})
        self.assertFalse(result.get("isError"), result)
        sprint.refresh_from_db()
        self.assertTrue(sprint.is_deleted)

    def test_a_read_key_cannot_call_them_even_from_a_cached_list(self):
        self._key(scope=SCOPE_READ)
        sprint = self.make()
        for name, args in (
            ("manage_sprint", {"action": "update", "sprint_id": sprint.sprint_id, "name": "x"}),
            ("delete_sprint", {"sprint_id": sprint.sprint_id}),
        ):
            result, _ = self.run_tool(name, args)
            self.assertTrue(result.get("isError"), name)
        sprint.refresh_from_db()
        self.assertEqual(sprint.name, "Sprint 1")
        self.assertFalse(sprint.is_deleted)

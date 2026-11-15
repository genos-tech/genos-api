"""MIP-1583: six weaknesses of the app's milestone convert and project-move code.

Found while the agent and MCP got the same two operations (MIP-1564): the tool calls the app's own
conversion service and move code, so each weakness was the app's too. One class per item.
"""

from __future__ import annotations

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_agent_parity_milestones import ToolBase

MILESTONE_URL = "/api/v2/milestone/"
TO_TASK = "/api/v2/milestone/convert-to-task/"


class SubTasksKeptCountsLiveTasks(ToolBase):
    """Item 1: `subTasksKept` is what a person still sees under the task, not the rows."""

    def deleted_world(self):
        m = self.roadmap_milestone()  # First, Second and First's child
        # The app's own delete is the status; a soft-deleted row is what a milestone delete leaves.
        TaskMaster.objects.filter(title="Second").update(is_deleted=True, status="Deleted")
        TaskMaster.objects.filter(title="First's child").update(status="Deleted")
        return m

    def test_the_app_counts_only_the_live_tasks(self):
        m = self.deleted_world()
        res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["subTasksKept"], 1)

    def test_the_tool_says_the_same(self):
        m = self.deleted_world()
        out = self.to_task_tool(m)
        self.assertEqual(out["sub_tasks_kept"], 1)
        self.assertIn("1 task(s) stay under it", out["__summary__"])

    def test_a_deleted_task_still_leaves_the_milestone(self):
        # Only the count changes: every row that cited the milestone lets go of it.
        m = self.deleted_world()
        self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertFalse(TaskMaster.objects.filter(milestone_id=m.milestone_id).exists())
        self.assertTrue(MilestoneMaster.objects.get(pk=m.pk).is_deleted)

    def test_nothing_deleted_counts_every_task(self):
        m = self.roadmap_milestone()
        res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertEqual(res.data["subTasksKept"], 3)

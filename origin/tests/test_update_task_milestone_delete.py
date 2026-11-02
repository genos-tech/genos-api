"""`update_task` refuses to delete a milestone (MIP-876).

A milestone is a `MilestoneMaster` row plus a backing `TaskMaster` row,
and `update_task` on the backing row mirrors `status` across both. For
`Deleted` that is only half of what `MilestoneView.delete` does: the
milestone kept `is_deleted=False` (so `list_milestones` still returned it,
as "Deleted"), its tasks stayed attached, and nothing purged the search
index. Four such milestones exist in production.

Contract: the call is refused with nothing changed, whatever else it
carries, and every other status and every ordinary task is unaffected.
"""

from origin.search_engine.agent.tools import ToolError
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK

from .test_agent_tool_task_milestone_move import MilestoneMoveTestBase


class UpdateTaskMilestoneDeleteTests(MilestoneMoveTestBase):
    def _delete(self, task_id, **extra):
        return UPDATE_TASK.run({"task_id": task_id, "status": "Deleted", **extra}, self.ctx)

    def test_deleting_a_milestone_is_refused_and_nothing_changes(self):
        child = self._task("child", milestone=self.ms_b, parent_task_id=self.ms_b.task_id)

        with self.assertRaises(ToolError) as caught:
            self._delete(self.ms_b.task_id)

        self.assertIn("milestone", str(caught.exception))
        self.assertIn("Nothing was changed", str(caught.exception))
        self.ms_b.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(self.ms_b.status, "Open")
        self.assertFalse(self.ms_b.is_deleted)
        self.assertEqual(child.milestone_id, self.ms_b.milestone_id)
        self.assertEqual(child.parent_task_id, self.ms_b.task_id)
        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        statuses = {m["title"]: m["status"] for m in listed["milestones"]}
        self.assertEqual(statuses["MS B"], "Open")

    def test_other_changes_in_the_same_call_are_not_applied(self):
        with self.assertRaises(ToolError):
            self._delete(self.ms_b.task_id, title="Renamed", priority="Critical")

        self.ms_b.refresh_from_db()
        self.assertEqual(self.ms_b.title, "MS B")
        self.assertNotEqual(self.ms_b.priority, "Critical")

    def test_the_other_statuses_still_work_on_a_milestone(self):
        UPDATE_TASK.run({"task_id": self.ms_b.task_id, "status": "Closed"}, self.ctx)

        self.ms_b.refresh_from_db()
        self.assertEqual(self.ms_b.status, "Closed")

    def test_an_ordinary_task_can_still_be_deleted(self):
        task = self._task("plain")

        self._delete(task.task_id)

        task.refresh_from_db()
        self.assertEqual(task.status, "Deleted")

    def test_a_task_inside_a_milestone_can_still_be_deleted(self):
        child = self._task("child", milestone=self.ms_b, parent_task_id=self.ms_b.task_id)

        self._delete(child.task_id)

        child.refresh_from_db()
        self.assertEqual(child.status, "Deleted")

    def test_the_bulk_tool_still_refuses_deleted(self):
        with self.assertRaises(ToolError):
            UPDATE_TASKS_BULK.run(
                {
                    "updates": [
                        {"task_id": self.ms_b.task_id, "status": "Deleted", "rationale": "x"}
                    ]
                },
                self.ctx,
            )

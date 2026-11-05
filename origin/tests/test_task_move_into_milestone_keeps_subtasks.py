"""MIP-1026: moving a root task into a milestone must carry its sub-tasks.

Reported: sub-tasks created over MCP, then the root task filed under a
milestone, and the sub-tasks vanished from the task table. The question
this file settles on the server is whether the ROWS are right afterwards
(`milestone_id`, `root_task_id` on every sub-task, `parent_task_id`
untouched) for both ways a task gets moved:

  * the task preview's milestone picker, which PUTs `milestone` plus the
    milestone's backing task as `parent_task_id`;
  * MCP `update_task` with `milestone_id`.

And that a root a client SENDS on create never wins over the one derived
from the parent (the second half of the report: "root task id should
always match the parent's").
"""

from __future__ import annotations

from rest_framework.test import APIClient

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_mcp_task_hierarchy import McpHierarchyBase


class _Fixture(McpHierarchyBase):
    """A project, a milestone, and a root task with three levels beneath it."""

    def setUp(self):
        super().setUp()
        self.web = APIClient()
        self.web.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token()}")

        backing = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="The milestone",
            status="Open",
            is_milestone=True,
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            task=backing,
            title="The milestone",
            reporter=self.user,
        )
        # A root task and its sub-tasks, all filed the way MCP files them.
        self.root = self._create("A root task")
        self.child_a = self._create("Sub-task A", parent=self.root)
        self.child_b = self._create("Sub-task B", parent=self.root)
        self.grandchild = self._create("Grand-child", parent=self.child_a)

    def _create(self, title, parent=None):
        args = {"project_id": self.project.project_id, "title": title}
        if parent is not None:
            args["parent_task_id"] = str(parent.task_id)
        text, is_error = self._text("create_task", args)
        self.assertFalse(is_error, text)
        return TaskMaster.objects.get(project=self.project, title=title)

    def _refresh(self):
        for row in (self.root, self.child_a, self.child_b, self.grandchild):
            row.refresh_from_db()


class MoveIntoMilestoneKeepsSubtasks(_Fixture):
    def _assert_subtree_follows(self):
        self._refresh()
        backing = self.milestone.task_id
        self.assertEqual(self.root.milestone_id, self.milestone.milestone_id)
        self.assertEqual(self.root.parent_task_id, backing)
        self.assertEqual(self.root.root_task_id, backing)
        for row in (self.child_a, self.child_b, self.grandchild):
            self.assertEqual(
                row.milestone_id, self.milestone.milestone_id, f"{row.title}: milestone_id"
            )
            self.assertEqual(row.root_task_id, backing, f"{row.title}: root_task_id")
        # The edges inside the moved sub-tree do not change.
        self.assertEqual(self.child_a.parent_task_id, self.root.task_id)
        self.assertEqual(self.child_b.parent_task_id, self.root.task_id)
        self.assertEqual(self.grandchild.parent_task_id, self.child_a.task_id)

    def test_mcp_created_subtasks_root_at_their_parents_root(self):
        for row in (self.child_a, self.child_b, self.grandchild):
            self.assertEqual(row.root_task_id, self.root.task_id, row.title)

    def test_the_previews_milestone_picker_carries_the_subtree(self):
        res = self.web.put(
            "/api/v2/task/",
            {
                "task_id": self.root.task_id,
                "project": self.project.project_id,
                "milestone": self.milestone.milestone_id,
                "parent_task_id": self.milestone.task_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self._assert_subtree_follows()

    def test_mcp_update_task_milestone_carries_the_subtree(self):
        text, is_error = self._text(
            "update_task",
            {"task_id": str(self.root.task_id), "milestone_id": str(self.milestone.milestone_id)},
        )
        self.assertFalse(is_error, text)
        self._assert_subtree_follows()

    def test_the_project_task_list_returns_the_moved_subtree(self):
        self.web.put(
            "/api/v2/task/",
            {
                "task_id": self.root.task_id,
                "project": self.project.project_id,
                "milestone": self.milestone.milestone_id,
                "parent_task_id": self.milestone.task_id,
            },
            format="json",
        )
        rows = {
            r["title"]: r
            for r in self._project_rows()
            if r["title"] in {"A root task", "Sub-task A", "Sub-task B", "Grand-child"}
        }
        self.assertEqual(len(rows), 4, rows.keys())
        for title in ("Sub-task A", "Sub-task B", "Grand-child"):
            self.assertEqual(rows[title]["milestoneId"], self.milestone.milestone_id, title)
            self.assertEqual(rows[title]["rootTaskId"], self.milestone.task_id, title)

    def _project_rows(self):
        res = self.web.get(
            f"/api/v2/task/getProjectTasks/?team_id={self.team.team_id}&project_id={self.project.project_id}"
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res.data["data"]["tasks"]


class CreateDerivesItsRootFromTheParent(_Fixture):
    """A task's root is a consequence of where it hangs, so a root the
    client SENDS on create is ignored (MIP-1026, decided with Ken: ignore
    it quietly, no 400). Before, the post-save signal skipped derivation
    whenever a root was already set, so a browser holding a stale tree
    stored the stale root for good."""

    def _payload(self, **overrides):
        payload = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "Created in the app",
            "priority": "Medium",
            "effort_level": "Medium",
            "status": "Open",
            "content": None,
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        payload.update(overrides)
        return payload

    def _post(self, **overrides):
        res = self.web.post("/api/v2/task/", self._payload(**overrides), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return TaskMaster.objects.get(task_id=res.data["task"]["task_id"])

    def test_a_sent_root_loses_to_the_parents_root(self):
        # Parent chain root -> child_a; the client claims a different root.
        new = self._post(parent_task_id=self.child_a.task_id, root_task_id=self.milestone.task_id)
        self.assertEqual(new.parent_task_id, self.child_a.task_id)
        self.assertEqual(new.root_task_id, self.root.task_id)

    def test_a_sent_root_without_a_parent_is_the_task_itself(self):
        new = self._post(root_task_id=self.root.task_id)
        self.assertIsNone(new.parent_task_id)
        self.assertEqual(new.root_task_id, new.task_id)

    def test_a_milestone_create_roots_at_the_backing_task(self):
        new = self._post(milestone=self.milestone.milestone_id, root_task_id=self.root.task_id)
        self.assertEqual(new.parent_task_id, self.milestone.task_id)
        self.assertEqual(new.root_task_id, self.milestone.task_id)

    def test_a_create_without_a_root_is_unchanged(self):
        new = self._post(parent_task_id=self.child_b.task_id)
        self.assertEqual(new.root_task_id, self.root.task_id)

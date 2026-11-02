"""`delete_milestone` (MIP-879).

A milestone delete is not a status change: `MilestoneView.delete` marks the
milestone and its backing row deleted, detaches the milestone's tasks and
whatever hangs off the backing row, drops the project's task cache and purges
the search index. `update_task` refuses `status: 'Deleted'` on a milestone for
exactly that reason (MIP-876), so the delete has to be its own tool, and it has
to leave what the UI's DELETE leaves. The core test drives both on twin
milestones and compares the milestone, its backing row, every task it held and
those tasks' history.
"""

from __future__ import annotations

from unittest import mock

from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolError, ToolForbidden
from origin.search_engine.agent.tools.delete_milestone import DELETE_MILESTONE
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

from .test_agent_tool_manage_milestone import MilestoneToolBase

SERVICE = "origin.services.milestone_manage"


def _task_after(task):
    """Where a task sits and what its history says, ids that differ between twins left out."""
    task = TaskMaster.objects.get(pk=task.pk)
    return {
        "title": task.title,
        "status": task.status,
        "is_deleted": task.is_deleted,
        "milestone_id": task.milestone_id,
        # By title: the ids differ between twins.
        "parent": TaskMaster.objects.get(task_id=task.parent_task_id).title
        if task.parent_task_id
        else None,
        "sprint_id": task.sprint_id,
        "project_id": task.project_id,
        "history": sorted(
            (a.action_type, str(a.old_value) != "None", str(a.new_value), a.metadata.get("cascade"))
            for a in TaskActivity.objects.filter(task=task)
            if a.action_type != "created"
        ),
    }


def _milestone_after(milestone):
    m = MilestoneMaster.objects.select_related("task").get(pk=milestone.pk)
    return {
        "is_deleted": m.is_deleted,
        "status": m.status,
        "title": m.title,
        "sprint_id": m.sprint_id,
        "folder_id": m.folder_id,
        "assignees": sorted(
            str(u) for u in m.milestone_assignees.values_list("user_id", flat=True)
        ),
        "backing": {
            "is_deleted": m.task.is_deleted,
            "status": m.task.status,
            "milestone_id": m.task.milestone_id,
            "parent_task_id": m.task.parent_task_id,
        },
    }


class DeleteMilestoneBase(MilestoneToolBase):
    def _populated(self, title="Doomed"):
        """A milestone in a sprint and a folder, with assignees and a small tree:
        top (in the milestone) > child, a loose task that only cites the
        milestone, and one already Deleted."""
        m = self._milestone(title, sprint=self.sprint, folder=self.folder)
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)
        top = self._task("top", milestone=m, parent_task_id=m.task_id, sprint=self.sprint)
        child = self._task("child", milestone=m, parent_task_id=top.task_id, sprint=self.sprint)
        loose = self._task("loose", milestone=m, sprint=self.sprint)
        gone = self._task("gone", milestone=m, parent_task_id=m.task_id, sprint=self.sprint)
        TaskMaster.objects.filter(pk=gone.pk).update(status="Deleted")
        return m, [top, child, loose, gone]

    def _delete(self, milestone, **extra):
        return DELETE_MILESTONE.run({"milestone_id": milestone.milestone_id, **extra}, self.ctx)


class DeleteMilestoneTwinTests(DeleteMilestoneBase):
    def test_delete_matches_the_uis_delete(self):
        by_tool, tool_tasks = self._populated("Doomed")
        by_ui, ui_tasks = self._populated("Doomed")

        with (
            mock.patch(f"{SERVICE}.purge_milestone") as tool_purge_milestone,
            mock.patch(f"{SERVICE}.purge_task") as tool_purge_task,
            mock.patch(f"{SERVICE}.invalidate_project_tasks_cache") as tool_cache,
        ):
            out = self._delete(by_tool)
        with (
            mock.patch(f"{SERVICE}.purge_milestone") as ui_purge_milestone,
            mock.patch(f"{SERVICE}.purge_task") as ui_purge_task,
            mock.patch(f"{SERVICE}.invalidate_project_tasks_cache") as ui_cache,
        ):
            res = self.client.delete(f"/api/v2/milestone/{by_ui.milestone_id}/")
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(_milestone_after(by_tool), _milestone_after(by_ui))
        self.assertEqual([_task_after(t) for t in tool_tasks], [_task_after(t) for t in ui_tasks])
        # The search index forgets the milestone and its backing row; the
        # project's task table is told to re-read.
        tool_purge_milestone.assert_called_once_with(by_tool.milestone_id)
        tool_purge_task.assert_called_once_with(by_tool.task_id)
        ui_purge_milestone.assert_called_once_with(by_ui.milestone_id)
        ui_purge_task.assert_called_once_with(by_ui.task_id)
        tool_cache.assert_called_once_with(self.team.team_id, self.project.project_id)
        ui_cache.assert_called_once_with(self.team.team_id, self.project.project_id)
        # What it did, spelled out (so the comparison above can't pass on two
        # untouched milestones).
        after = _milestone_after(by_tool)
        self.assertTrue(after["is_deleted"])
        self.assertTrue(after["backing"]["is_deleted"])
        self.assertEqual(after["backing"]["status"], "Deleted")
        self.assertTrue(out["deleted"])
        self.assertFalse(out["already_deleted"])

    def test_the_tasks_are_kept_and_detached(self):
        m, tasks = self._populated()
        top, child, loose, gone = tasks

        out = self._delete(m)

        for task in (top, child, loose):
            task.refresh_from_db()
            self.assertFalse(task.is_deleted)
            self.assertIsNone(task.milestone_id)
            self.assertNotEqual(task.status, "Deleted")
        self.assertIsNone(top.parent_task_id)  # was the backing row
        self.assertEqual(child.parent_task_id, top.task_id)  # still under its own parent
        # They are listed as detached: the live ones, not the one already Deleted.
        self.assertEqual(out["tasks_detached"], 3)
        self.assertIn("3 task(s) were kept", out["__summary__"])

    def test_the_detach_is_in_each_tasks_history_as_a_cascade(self):
        m, (top, child, loose, _gone) = self._populated()
        self._delete(m)
        for task in (top, child, loose):
            row = TaskActivity.objects.get(task=task, action_type="milestone_changed")
            self.assertEqual(row.metadata["cascade"], "milestone_delete")

    def test_the_milestone_disappears_from_the_reads(self):
        m, _ = self._populated()
        self._delete(m)

        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        self.assertNotIn(m.milestone_id, [r["milestone_id"] for r in listed["milestones"]])
        with self.assertRaises(ToolError) as caught:
            MANAGE_MILESTONE.run(
                {"action": "update", "milestone_id": m.milestone_id, "title": "Zombie"}, self.ctx
            )
        self.assertIn("has been deleted", str(caught.exception))

    def test_the_result_names_what_was_deleted(self):
        m, _ = self._populated("Named")
        out = self._delete(m)
        self.assertEqual(out["milestone_id"], m.milestone_id)
        self.assertEqual(out["task_id"], m.task_id)
        self.assertEqual(out["title"], "Named")
        self.assertEqual(out["project_id"], self.project.project_id)
        self.assertIn("Named", out["__summary__"])

    def test_an_empty_milestone_deletes_cleanly(self):
        m = self._milestone("Empty")
        out = self._delete(m)
        self.assertEqual(out["tasks_detached"], 0)
        self.assertTrue(MilestoneMaster.objects.get(pk=m.pk).is_deleted)

    def test_a_legacy_milestone_without_a_backing_task_deletes(self):
        legacy = self._milestone("Legacy", backed=False)
        out = self._delete(legacy)
        legacy.refresh_from_db()
        self.assertTrue(legacy.is_deleted)
        self.assertTrue(out["deleted"])
        self.assertIsNone(out["task_id"])

    def test_deleting_twice_succeeds_and_does_nothing_the_second_time(self):
        m, _ = self._populated()
        self._delete(m)
        with (
            mock.patch(f"{SERVICE}.purge_milestone") as purged,
            mock.patch(
                "origin.search_engine.agent.tools.delete_milestone.delete_milestone"
            ) as service,
        ):
            out = self._delete(m)
        self.assertTrue(out["deleted"])
        self.assertTrue(out["already_deleted"])
        self.assertIn("already deleted", out["__summary__"])
        service.assert_not_called()
        purged.assert_not_called()

    def test_a_failure_while_deleting_does_not_leak_the_error(self):
        m, _ = self._populated()
        with mock.patch(
            "origin.search_engine.agent.tools.delete_milestone.delete_milestone",
            side_effect=RuntimeError("db went away"),
        ):
            with self.assertRaises(ToolError) as caught:
                self._delete(m)
        self.assertNotIn("db went away", str(caught.exception))
        self.assertIn("ref", str(caught.exception))


class DeleteMilestoneAccessTests(DeleteMilestoneBase):
    def setUp(self):
        super().setUp()
        self.hidden_project = self._project_i_am_not_in()
        self.hidden = MilestoneMaster.objects.create(
            team=self.team, project=self.hidden_project, title="Hidden", reporter=self.user2
        )
        self.other_team, self.other_project = self._other_team_project()
        self.foreign = MilestoneMaster.objects.create(
            team=self.other_team, project=self.other_project, title="Foreign", reporter=self.user2
        )

    def test_a_milestone_the_caller_cannot_see_looks_like_a_miss_and_is_not_deleted(self):
        for milestone in (self.hidden, self.foreign):
            with self.subTest(milestone=milestone.title):
                with self.assertRaises(ToolForbidden) as caught:
                    self._delete(milestone)
                self.assertEqual(
                    str(caught.exception), f"Milestone {milestone.milestone_id} not found."
                )
                milestone.refresh_from_db()
                self.assertFalse(milestone.is_deleted)
        with self.assertRaises(ToolError) as missing:
            DELETE_MILESTONE.run({"milestone_id": 987654}, self.ctx)
        self.assertEqual(str(missing.exception), "Milestone 987654 not found.")

    def test_nothing_is_purged_or_detached_for_a_refused_delete(self):
        kid = self._task("kid", milestone=self.hidden, parent_task_id=self.hidden.task_id)
        with mock.patch(f"{SERVICE}.purge_milestone") as purged:
            with self.assertRaises(ToolError):
                self._delete(self.hidden)
        purged.assert_not_called()
        kid.refresh_from_db()
        self.assertEqual(kid.milestone_id, self.hidden.milestone_id)

    def test_a_deleted_one_in_a_project_the_caller_cannot_see_is_still_a_miss(self):
        """'Already deleted' must not be a way to learn a milestone exists."""
        MilestoneMaster.objects.filter(pk=self.hidden.pk).update(is_deleted=True)
        with self.assertRaises(ToolForbidden):
            self._delete(self.hidden)

    def test_the_id_must_be_a_number(self):
        for bad in ("abc", None, ""):
            with self.assertRaises(ToolError):
                DELETE_MILESTONE.run({"milestone_id": bad}, self.ctx)


class UpdateTaskStillRefusesTests(DeleteMilestoneBase):
    """`delete_milestone` exists now, and `update_task` still must not do half of it (MIP-876)."""

    def test_update_task_still_refuses_to_delete_a_milestone(self):
        m, (top, *_rest) = self._populated()
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASK.run({"task_id": m.task_id, "status": "Deleted"}, self.ctx)
        self.assertIn("update_task can't delete one", str(caught.exception))
        self.assertIn("Nothing was changed", str(caught.exception))
        m.refresh_from_db()
        self.assertFalse(m.is_deleted)
        self.assertEqual(m.status, "Open")
        top.refresh_from_db()
        self.assertEqual(top.milestone_id, m.milestone_id)

    def test_manage_milestone_refuses_a_deleted_status_too(self):
        m = self._milestone("Keep")
        with self.assertRaises(ToolError):
            MANAGE_MILESTONE.run(
                {"action": "update", "milestone_id": m.milestone_id, "status": "Deleted"}, self.ctx
            )
        m.refresh_from_db()
        self.assertEqual(m.status, "Open")
        self.assertFalse(m.is_deleted)

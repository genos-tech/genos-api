"""A deleted milestone reaches the browsers that already hold it (MIP-936).

A browser keeps the project's tasks in its own store and syncs by asking for
the rows CHANGED SINCE its checkpoint (`getProjectTasks?since=`): a row counts
as changed when its `ts_updated_at` is newer. That column is `auto_now`, which
`.save()` refreshes and a queryset `.update()` does not. The milestone delete
and the cascades behind `update_tasks_recorded` are queryset updates, so for a
browser that had loaded the milestone the delete was invisible: the milestone
stayed on screen, and the UI's own delete then found it already gone.

The checkpoint is simulated the way the browser takes it: a time read just
before the change, after the browser's last full load.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.delete_milestone import DELETE_MILESTONE
from origin.services.activity_suppression import suppress_task_activity
from origin.signals.task_signals import update_tasks_recorded

from .test_agent_tool_manage_milestone import MilestoneToolBase

PROJECT_TASKS = "/api/v2/task/getProjectTasks/"


class BrowserSyncBase(MilestoneToolBase):
    def _load(self, since=None) -> dict:
        params = {"team_id": str(self.team.team_id), "project_id": str(self.project.project_id)}
        if since is not None:
            params["since"] = since.isoformat()
        res = self.client.get(PROJECT_TASKS, params)
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return {t["id"]: t for t in res.data["data"]["tasks"]}

    def _milestone_with_tasks(self):
        m = self._milestone("Doomed")
        top = self._task("top", milestone=m, parent_task_id=m.task_id)
        child = self._task("child", milestone=m, parent_task_id=top.task_id)
        return m, top, child


class DeletedMilestoneReachesBrowsersTests(BrowserSyncBase):
    def test_the_backing_row_arrives_as_a_tombstone(self):
        m, _, _ = self._milestone_with_tasks()
        backing = str(m.task_id)
        self.assertIn(backing, self._load())  # the browser holds it
        checkpoint = timezone.now()

        DELETE_MILESTONE.run({"milestone_id": m.milestone_id}, self.ctx)

        delta = self._load(checkpoint)
        self.assertIn(backing, delta)
        self.assertTrue(delta[backing]["isDeleted"])
        # And a fresh load agrees: it is not there at all.
        self.assertNotIn(backing, self._load())

    def test_the_ui_delete_does_the_same(self):
        m, _, _ = self._milestone_with_tasks()
        backing = str(m.task_id)
        checkpoint = timezone.now()

        with (
            mock.patch("origin.services.milestone_manage.purge_milestone"),
            mock.patch("origin.services.milestone_manage.purge_task"),
        ):
            res = self.client.delete(f"/api/v2/milestone/{m.milestone_id}/")
        self.assertEqual(res.status_code, 200, res.data)

        delta = self._load(checkpoint)
        self.assertTrue(delta[backing]["isDeleted"])

    def test_the_detached_tasks_arrive_detached(self):
        m, top, child = self._milestone_with_tasks()
        self.assertEqual(self._load()[str(top.task_id)]["milestoneId"], m.milestone_id)
        checkpoint = timezone.now()

        DELETE_MILESTONE.run({"milestone_id": m.milestone_id}, self.ctx)

        delta = self._load(checkpoint)
        # `top` hung off the backing row and cited the milestone: it is a
        # root now, with no milestone. Without the stamp the browser kept
        # showing it under the deleted milestone.
        self.assertIn(str(top.task_id), delta)
        self.assertIsNone(delta[str(top.task_id)]["milestoneId"])
        self.assertIsNone(delta[str(top.task_id)]["parentTaskId"])
        # `child` kept its parent but lost the milestone.
        self.assertIn(str(child.task_id), delta)
        self.assertIsNone(delta[str(child.task_id)]["milestoneId"])


class CascadeStampTests(BrowserSyncBase):
    """`update_tasks_recorded` is the shared door for the cascades (milestone
    move, sprint push, delete, project move), so the stamp lives there."""

    def _aged(self, task):
        """The task as a browser saw it long ago: `ts_updated_at` pushed back
        with a queryset update, the only write that leaves it alone."""
        old = timezone.now() - timedelta(days=3)
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=old)
        return old

    def test_a_tracked_cascade_stamps_the_rows_it_touches(self):
        m, top, _ = self._milestone_with_tasks()
        old = self._aged(top)

        update_tasks_recorded(TaskMaster.objects.filter(pk=top.pk), cascade="t", milestone=None)

        self.assertGreater(TaskMaster.objects.get(pk=top.pk).ts_updated_at, old)

    def test_it_is_stamped_while_activity_is_suppressed_too(self):
        # The bulk teardown path skips the audit rows but the browsers still
        # need to hear about the change.
        m, top, _ = self._milestone_with_tasks()
        old = self._aged(top)

        with suppress_task_activity():
            update_tasks_recorded(TaskMaster.objects.filter(pk=top.pk), cascade="t", milestone=None)

        self.assertGreater(TaskMaster.objects.get(pk=top.pk).ts_updated_at, old)

    def test_rows_it_does_not_touch_keep_their_stamp(self):
        m, top, child = self._milestone_with_tasks()
        old = self._aged(child)

        update_tasks_recorded(TaskMaster.objects.filter(pk=top.pk), cascade="t", milestone=None)

        self.assertEqual(TaskMaster.objects.get(pk=child.pk).ts_updated_at, old)

    def test_a_caller_that_names_its_own_stamp_keeps_it(self):
        m, top, _ = self._milestone_with_tasks()
        mine = timezone.now() + timedelta(days=1)

        update_tasks_recorded(
            TaskMaster.objects.filter(pk=top.pk), cascade="t", milestone=None, ts_updated_at=mine
        )

        self.assertEqual(TaskMaster.objects.get(pk=top.pk).ts_updated_at, mine)

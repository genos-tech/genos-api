"""MIP-1361: `get_stale_tasks` counts progress UNDER a task as activity.

A milestone or parent whose sub-tasks move every day is not stuck, however long
since its own row was edited (`ts_updated_at`). "Quiet" is now both that and
`last_progress_at` (MIP-1351) being older than the cutoff; the oldest of the two
is what `days_stale` reports and what the list is ordered by.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.tests.test_base import BaseAPITestCase


class GetStaleTasksProgressTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Stale", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, title, *, edited, progress=None, status="Open"):
        """A task whose own last edit was `edited` days ago and whose last progress was
        `progress` days ago (None: nothing recorded)."""
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status=status
        )
        now = timezone.now()
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=now - timedelta(days=edited),
            last_progress_at=None if progress is None else now - timedelta(days=progress),
        )
        return task

    def _stale(self, stale_days=7, **extra):
        out = REGISTRY["get_stale_tasks"].run(
            {"stale_days": stale_days, "project_id": self.project.project_id, **extra}, self.ctx
        )
        return {t["task_id"]: t for t in out["tasks"]}, out

    def test_a_parent_whose_children_are_moving_is_not_stale(self):
        parent = self._task("Milestone", edited=30, progress=2)
        rows, _ = self._stale()
        self.assertNotIn(parent.task_id, rows)

    def test_a_task_quiet_on_both_counts_is_stale_and_reports_the_later_one(self):
        quiet = self._task("Quiet", edited=30, progress=10)
        rows, _ = self._stale()
        self.assertEqual(rows[quiet.task_id]["days_stale"], 10)
        self.assertIsNotNone(rows[quiet.task_id]["last_progress_at"])

    def test_nothing_recorded_falls_back_to_its_own_edits(self):
        old = self._task("No progress yet", edited=30)
        fresh = self._task("Edited lately", edited=2)
        rows, _ = self._stale()
        self.assertEqual(rows[old.task_id]["days_stale"], 30)
        self.assertIsNone(rows[old.task_id]["last_progress_at"])
        self.assertNotIn(fresh.task_id, rows)

    def test_a_task_edited_lately_is_not_stale_even_if_progress_is_old(self):
        edited = self._task("Edited", edited=1, progress=40)
        rows, _ = self._stale()
        self.assertNotIn(edited.task_id, rows)

    def test_oldest_activity_first(self):
        """Ordered by the later of the two, not by the task's own edit: here that
        is b (20 days), c (15) then a (9)."""
        a = self._task("a", edited=30, progress=9)
        b = self._task("b", edited=20, progress=None)
        c = self._task("c", edited=60, progress=15)
        out = REGISTRY["get_stale_tasks"].run(
            {"stale_days": 7, "project_id": self.project.project_id}, self.ctx
        )
        self.assertEqual([t["task_id"] for t in out["tasks"]], [b.task_id, c.task_id, a.task_id])

    def test_the_limit_counts_only_tasks_that_are_really_stale(self):
        """Parents with children moving used to crowd the list; they are filtered
        before the limit, so a task that is stale still makes it in."""
        for n in range(3):
            self._task(f"busy {n}", edited=90, progress=1)
        stuck = self._task("stuck", edited=40)
        rows, out = self._stale(limit=1)
        self.assertEqual(list(rows), [stuck.task_id])

    def test_closed_tasks_are_still_left_out(self):
        done = self._task("done", edited=30, status="Closed")
        rows, _ = self._stale()
        self.assertNotIn(done.task_id, rows)

"""Task-write tools must bump `ts_updated_at`.

`TaskMaster.ts_updated_at` is `auto_now=True`, but Django only calls
`pre_save()` on fields listed in `update_fields` — so a
`save(update_fields=["status"])` silently leaves the timestamp at its
old value. The REST path knows this and lists the column explicitly
(`task_views.py:127`, `:689`); the agent tools did not.

That is not cosmetic. `ts_updated_at` is the incremental reindexer's
watermark (`task_chunker` filters `ts_updated_at__gte=since`, driven by
`opensearch_reindex --since-minutes`), so a task the agent moved to WIP
stayed stale in search until something else touched it. It is also the
ordering key for `list_tasks`, so agent-edited tasks did not float to
the top of "recently updated".

These tests assert the observable consequence — the timestamp advances —
rather than the `update_fields` list, so they keep holding if the tools
switch to a different save strategy.
"""

from datetime import date, timedelta

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK

from .test_base import BaseAPITestCase


class WriteToolsTouchUpdatedAtTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, **kwargs):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=kwargs.pop("title", "Implementation"),
            status=kwargs.pop("status", "Open"),
            priority=kwargs.pop("priority", "Normal"),
            **kwargs,
        )
        # Backdate past any reindex window so "did it move" is unambiguous
        # — .update() bypasses auto_now, which is the point.
        self.stale = task.ts_updated_at - timedelta(days=3)
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=self.stale)
        task.refresh_from_db()
        return task

    def _assert_advanced(self, task):
        task.refresh_from_db()
        self.assertGreater(
            task.ts_updated_at,
            self.stale,
            "ts_updated_at did not move — the row is invisible to the incremental reindexer",
        )

    def test_update_task_status_bumps_updated_at(self):
        """The exact case the MCP loop hits: an agent moves a task to WIP."""
        task = self._task()
        UPDATE_TASK.run({"task_id": task.task_id, "status": "WIP"}, self.ctx)
        self._assert_advanced(task)

    def test_update_task_due_date_bumps_updated_at(self):
        task = self._task()
        UPDATE_TASK.run({"task_id": task.task_id, "due_date": "2026-09-01"}, self.ctx)
        self._assert_advanced(task)

    def test_assign_task_bumps_updated_at(self):
        task = self._task()
        ASSIGN_TASK.run({"task_id": task.task_id, "assignee_id": str(self.user.id)}, self.ctx)
        self._assert_advanced(task)

    def test_update_tasks_bulk_bumps_updated_at(self):
        task = self._task()
        UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {
                        "task_id": task.task_id,
                        "priority": "High",
                        "rationale": "blocks the release",
                    }
                ]
            },
            self.ctx,
        )
        self._assert_advanced(task)

    def test_a_noop_update_does_not_touch_the_timestamp(self):
        """The tools skip the save entirely when nothing changed, and that
        must stay true — otherwise every no-op re-queues the row for
        reindexing."""
        task = self._task(status="Open", due_date=date(2026, 8, 1))
        UPDATE_TASK.run({"task_id": task.task_id, "status": "Open"}, self.ctx)
        task.refresh_from_db()
        self.assertEqual(task.ts_updated_at, self.stale)

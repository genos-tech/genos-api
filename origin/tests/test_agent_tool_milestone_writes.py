"""Spotlight write tools must persist milestone edits authoritatively.

A milestone is a task with `is_milestone=True`, but its canonical values
live on the `MilestoneMaster` side table; the backing `TaskMaster` row is
a mirror the project table reads and `sync_backing_task` overwrites on the
next milestone PATCH. The agent's `update_task` / `update_tasks_bulk`
tools operate on the milestone's backing task row.

Before the fix they wrote only `TaskMaster.status`, so:
  * the table (reads the backing row) showed the new status,
  * the preview (reads `MilestoneMaster.status`) kept the old one, and
  * the next milestone PATCH ran `sync_backing_task` and reverted the
    backing row to the stale milestone value — silent data loss.

These tests pin the observable contract: after an agent write, BOTH the
milestone and its backing task carry the new value, and a subsequent
`sync_backing_task` (the PATCH mirror pass) does NOT revert it. A plain
(non-milestone) task must keep behaving exactly as before.
"""

from datetime import date, timedelta

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services.milestone_service import ensure_backing_task, sync_backing_task

from .test_base import BaseAPITestCase


class MilestoneWriteToolsTests(BaseAPITestCase):
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

    def _milestone(self, **kwargs):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=kwargs.pop("title", "v1.0"),
            status=kwargs.pop("status", "Open"),
            priority=kwargs.pop("priority", "Normal"),
            **kwargs,
        )
        ensure_backing_task(milestone)
        return milestone

    def _plain_task(self, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=kwargs.pop("title", "Implementation"),
            status=kwargs.pop("status", "Open"),
            priority=kwargs.pop("priority", "Normal"),
            **kwargs,
        )

    # ---- update_task: the exact reported bug ------------------------

    def test_update_task_status_persists_on_milestone(self):
        """Open→WIP via update_task lands on the milestone AND its backing
        row — the bug was that only the backing row moved."""
        milestone = self._milestone(status="Open")
        backing = milestone.task

        UPDATE_TASK.run({"task_id": backing.task_id, "status": "WIP"}, self.ctx)

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.status, "WIP")
        self.assertEqual(backing.status, "WIP")

    def test_update_task_status_survives_later_sync(self):
        """A subsequent milestone PATCH (modelled by sync_backing_task)
        must NOT revert the agent's status — the data-loss half of the
        bug."""
        milestone = self._milestone(status="Open")
        backing = milestone.task

        UPDATE_TASK.run({"task_id": backing.task_id, "status": "WIP"}, self.ctx)

        # A later, unrelated milestone edit re-runs the mirror pass.
        milestone.refresh_from_db()
        sync_backing_task(milestone)

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.status, "WIP")
        self.assertEqual(backing.status, "WIP")

    def test_update_task_priority_and_due_date_persist_on_milestone(self):
        milestone = self._milestone(priority="Normal")
        backing = milestone.task
        due = (date.today() + timedelta(days=14)).isoformat()

        UPDATE_TASK.run(
            {"task_id": backing.task_id, "priority": "High", "due_date": due},
            self.ctx,
        )

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.priority, "High")
        self.assertEqual(milestone.due_date, date.fromisoformat(due))
        self.assertEqual(backing.priority, "High")
        self.assertEqual(backing.due_date, date.fromisoformat(due))

    def test_update_task_title_and_body_persist_on_milestone(self):
        """`content` on the task maps to `description` on the milestone."""
        milestone = self._milestone(title="v1.0")
        backing = milestone.task

        UPDATE_TASK.run(
            {"task_id": backing.task_id, "title": "v1.0 GA", "content_text": "Ship it"},
            self.ctx,
        )

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.title, "v1.0 GA")
        self.assertEqual(backing.title, "v1.0 GA")
        # Body mirrored across the differently-named columns.
        self.assertEqual(milestone.description, backing.content)
        self.assertTrue(milestone.description)

    def test_update_task_blocked_status_allowed_on_milestone(self):
        """Milestones DO support Blocked — the dependency automation and
        the aggregation tools already treat it as a live milestone status
        (see task_blocking.py). The tool must not special-case it away."""
        milestone = self._milestone(status="Open")
        backing = milestone.task

        UPDATE_TASK.run({"task_id": backing.task_id, "status": "Blocked"}, self.ctx)

        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "Blocked")

    def test_update_task_noop_leaves_milestone_untouched(self):
        milestone = self._milestone(status="Open")
        result = UPDATE_TASK.run(
            {"task_id": milestone.task.task_id, "status": "Open"}, self.ctx
        )
        self.assertEqual(result["changed_fields"], [])
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "Open")

    def test_update_task_plain_task_unaffected(self):
        """Non-milestone tasks keep the original single-row behaviour."""
        task = self._plain_task(status="Open")
        UPDATE_TASK.run({"task_id": task.task_id, "status": "WIP"}, self.ctx)
        task.refresh_from_db()
        self.assertEqual(task.status, "WIP")
        # No milestone got conjured for a plain task.
        self.assertFalse(MilestoneMaster.objects.filter(task_id=task.task_id).exists())

    # ---- update_tasks_bulk ------------------------------------------

    def test_bulk_update_status_persists_on_milestone(self):
        milestone = self._milestone(status="Open")
        backing = milestone.task

        UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {
                        "task_id": backing.task_id,
                        "status": "WIP",
                        "priority": "High",
                        "rationale": "sprint started",
                    }
                ]
            },
            self.ctx,
        )

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.status, "WIP")
        self.assertEqual(milestone.priority, "High")
        self.assertEqual(backing.status, "WIP")

    def test_bulk_update_survives_later_sync(self):
        milestone = self._milestone(status="Open")
        backing = milestone.task

        UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {"task_id": backing.task_id, "status": "WIP", "rationale": "started"}
                ]
            },
            self.ctx,
        )
        milestone.refresh_from_db()
        sync_backing_task(milestone)

        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "WIP")

    def test_bulk_update_mixes_milestone_and_plain_task(self):
        """One batch touching a milestone backing row and a plain task
        routes each to the right persistence path."""
        milestone = self._milestone(status="Open")
        plain = self._plain_task(status="Open")

        UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {"task_id": milestone.task.task_id, "status": "WIP", "rationale": "a"},
                    {"task_id": plain.task_id, "status": "Pending", "rationale": "b"},
                ]
            },
            self.ctx,
        )

        milestone.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual(milestone.status, "WIP")
        self.assertEqual(plain.status, "Pending")

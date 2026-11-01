"""A milestone's progress counts its live work (MIP-776, MIP-777).

Two things were counted that are not work: the milestone's own backing row (a
milestone planned with 20 tasks reported 21), and tasks whose status is
"Deleted", counted as CLOSED (progress 37/49 where the live figure is 33/44).
A Deleted task is gone, not finished: the task tree already hides it. So the UI
progress bar and every MCP/agent rollup count the same set, and `list_tasks`
returns exactly that set unless `Deleted` is asked for by name.
"""

from datetime import date, timedelta

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.get_sprint_summary import GET_SPRINT_SUMMARY
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.list_my_milestones import LIST_MY_MILESTONES
from origin.search_engine.agent.tools.list_sprints import LIST_SPRINTS
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.services.milestone_service import ensure_backing_task
from origin.views.task.milestone_views import _serialize_milestone

from .test_base import BaseAPITestCase


class MilestoneRollupTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date=date.today() - timedelta(days=3),
            end_date=date.today() + timedelta(days=10),
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1",
            status="Open",
            sprint=self.sprint,
        )
        self.backing = ensure_backing_task(self.milestone)
        MilestoneAssignees.objects.create(milestone=self.milestone, user=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        # 2 open, 3 closed (one attached by parent only), 2 deleted, + the backing row.
        self.open = [self._task(f"open{i}", status="Open") for i in range(2)]
        self.closed = [self._task(f"closed{i}", status="Closed") for i in range(2)]
        self.closed.append(
            self._task("closed-by-parent", status="Closed", milestone=None, parent=True)
        )
        self.deleted = [self._task(f"deleted{i}", status="Deleted") for i in range(2)]

    def _task(self, title, *, status, milestone="self", parent=False, **kw):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=title,
            status=status,
            sprint=self.sprint,
            milestone=self.milestone if milestone == "self" else None,
            parent_task_id=self.backing.task_id if parent else None,
            priority="Normal",
            effort_level="Low",
            **kw,
        )

    # the numbers every surface must agree on
    LIVE_TOTAL, LIVE_CLOSED, DELETED = 5, 3, 2

    def test_the_ui_progress_counts_live_work_only(self):
        out = _serialize_milestone(MilestoneMaster.objects.get(pk=self.milestone.pk))
        self.assertEqual(out["tasksTotal"], self.LIVE_TOTAL)
        self.assertEqual(out["tasksClosed"], self.LIVE_CLOSED)

    def test_list_milestones_agrees_with_the_ui(self):
        out = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        (row,) = out["milestones"]
        self.assertEqual((row["tasks_total"], row["tasks_closed"]), (5, 3))

    def test_list_my_milestones_agrees_with_the_ui(self):
        out = LIST_MY_MILESTONES.run({}, self.ctx)
        (row,) = out["milestones"]
        self.assertEqual((row["tasks_total"], row["tasks_closed"]), (5, 3))

    def test_get_milestone_summary_agrees_and_keeps_deleted_in_its_own_row(self):
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertEqual(out["total_tasks"], self.LIVE_TOTAL)
        self.assertEqual(out["status_breakdown"]["Closed"], self.LIVE_CLOSED)
        self.assertEqual(out["status_breakdown"]["Open"], 2)
        self.assertEqual(out["status_breakdown"]["Deleted"], self.DELETED)
        # The breakdowns add up to the total: Deleted and the backing row are in none.
        self.assertEqual(sum(out["priority_breakdown"].values()), self.LIVE_TOTAL)
        self.assertEqual(sum(out["effort_breakdown"].values()), self.LIVE_TOTAL)
        live = sum(v for k, v in out["status_breakdown"].items() if k != "Deleted")
        self.assertEqual(live, self.LIVE_TOTAL)
        self.assertIn("3 closed", out["__summary__"])

    def test_get_sprint_summary_milestone_rollup_and_totals(self):
        out = GET_SPRINT_SUMMARY.run({"sprint_id": self.sprint.sprint_id}, self.ctx)
        (m,) = out["milestones"]
        self.assertEqual((m["tasks_total"], m["tasks_closed"]), (5, 3))
        # Sprint level: tasks in the sprint (the milestone's backing row too),
        # without the Deleted ones, which keep their own row.
        self.assertEqual(out["status_breakdown"]["Deleted"], self.DELETED)
        self.assertEqual(out["status_breakdown"]["Closed"], self.LIVE_CLOSED)
        self.assertEqual(out["total_tasks"], self.LIVE_TOTAL + 1)  # + the milestone's own row

    def test_list_sprints_does_not_count_deleted_as_closed(self):
        out = LIST_SPRINTS.run({"project_id": self.project.project_id}, self.ctx)
        (row,) = out["sprints"]
        self.assertEqual(row["tasks_closed"], self.LIVE_CLOSED)
        self.assertEqual(row["tasks_total"], self.LIVE_TOTAL + 1)

    # list_tasks returns exactly the milestone's work
    def _listed(self, **args):
        out = LIST_TASKS.run(
            {"milestone_id": self.milestone.milestone_id, "limit": 50, **args}, self.ctx
        )
        return {t["title"] for t in out["tasks"]}, out

    def test_list_tasks_rows_add_up_to_the_milestones_total(self):
        titles, out = self._listed()
        self.assertEqual(len(titles), self.LIVE_TOTAL)
        self.assertNotIn("v1", titles)  # the milestone's own row
        self.assertFalse({"deleted0", "deleted1"} & titles)
        self.assertIn("closed-by-parent", titles)
        self.assertFalse(out["has_more"])

    def test_list_tasks_returns_deleted_only_when_asked_for_by_name(self):
        titles, _ = self._listed(status=["Deleted"])
        self.assertEqual(titles, {"deleted0", "deleted1"})
        titles, _ = self._listed(status=["Closed", "Deleted"])
        self.assertEqual(len(titles), self.LIVE_CLOSED + self.DELETED)

    def test_list_tasks_without_a_milestone_still_hides_deleted(self):
        out = LIST_TASKS.run({"project_id": self.project.project_id, "limit": 50}, self.ctx)
        statuses = {t["status"] for t in out["tasks"]}
        self.assertNotIn("Deleted", statuses)

    # Deleted is "not active" everywhere it was before: only "done" changed
    def test_a_deleted_task_is_still_not_overdue(self):
        TaskMaster.objects.filter(pk__in=[t.pk for t in self.deleted]).update(
            due_date=date.today() - timedelta(days=5)
        )
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertEqual(out["overdue_count"], 0)

    def test_a_deleted_blocker_still_does_not_block(self):
        blocked = self.open[0]
        TaskDependency.objects.create(
            blocker_task=self.deleted[0], blocked_task=blocked, team=self.team, created_by=self.user
        )
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertIn(blocked.title, [r["title"] for r in out["ready_now"]])

    def test_a_milestone_of_only_deleted_tasks_reads_zero_of_zero(self):
        TaskMaster.objects.filter(milestone=self.milestone).exclude(pk=self.backing.pk).update(
            status="Deleted"
        )
        TaskMaster.objects.filter(parent_task_id=self.backing.task_id).update(status="Deleted")
        out = _serialize_milestone(MilestoneMaster.objects.get(pk=self.milestone.pk))
        self.assertEqual((out["tasksTotal"], out["tasksClosed"]), (0, 0))
        summary = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertEqual(summary["total_tasks"], 0)

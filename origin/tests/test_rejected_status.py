"""The system status "Rejected" (MIP-2494): finished, but not done.

One test per kind of reader: progress, the closed-ancestor rule, dependencies, throughput,
webhooks, the REST writers, the lists, CSV import, the calendar title, the built-in project row
and its backfill. The agent and MCP surfaces are in `test_agent_custom_status`.
"""

from datetime import date, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from django.apps import apps

from origin.models.common.webhook_models import ALL_EVENTS, EVENT_TASK_REJECTED
from origin.models.project.prj_models import ProjectStatus
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_my_task_summary import GET_MY_TASK_SUMMARY
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.get_team_task_summary import GET_TEAM_TASK_SUMMARY
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.task_closes import CLOSE_Q
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services import calendar_sync, task_ancestry, task_import, webhook_enqueue
from origin.services.milestone_rollup import milestone_work_counts
from origin.services.milestone_service import create_milestone
from origin.services.project_statuses import ensure_project_statuses
from origin.services.task_status_scope import parse_statuses
from origin.services.task_status_values import status_value_error
from origin.tests.test_project_statuses import StatusTestBase

backfill = import_module("origin.migrations.0300_rejected_builtin_status_mip2494")


class RejectedBase(StatusTestBase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def milestone(self, title="M", status="Open"):
        return create_milestone(self.project, reporter_id=self.user.id, title=title, status=status)

    def in_milestone(self, m, status="Open", **extra):
        return self.task(
            status, milestone=m, parent_task_id=m.task_id, title=extra.pop("title", "w"), **extra
        )

    def reads(self, *tasks):
        found = task_ancestry.effective_statuses([t.task_id for t in tasks], self.team.team_id)
        return [found[t.task_id].status for t in tasks]

    def put(self, task, **body):
        return self.client.put("/api/v2/task/", {"task_id": task.task_id, **body}, format="json")


class ProgressTests(RejectedBase):
    def test_a_rejected_task_leaves_the_bar_without_counting_as_closed(self):
        m = self.milestone()
        self.in_milestone(m, "Closed")
        self.in_milestone(m, "Open")
        gone = self.in_milestone(m, "Open")
        for rule in (False, True):
            self.assertEqual(milestone_work_counts([m], ancestor_rule=rule)[m.milestone_id], (3, 1))
        gone.status = "Rejected"
        gone.save()
        for rule in (False, True):
            self.assertEqual(milestone_work_counts([m], ancestor_rule=rule)[m.milestone_id], (2, 1))

    def test_the_sub_tasks_of_a_rejected_task_are_not_progress_but_done_work_stays_done(self):
        m = self.milestone()
        root = self.in_milestone(m, "Rejected")
        self.task("Open", parent_task_id=root.task_id, milestone=m)
        self.task("Closed", parent_task_id=root.task_id, milestone=m)
        self.assertEqual(milestone_work_counts([m], ancestor_rule=True)[m.milestone_id], (1, 1))

    def test_a_rejected_task_under_a_closed_milestone_does_not_count(self):
        m = self.milestone(status="Closed")
        self.in_milestone(m, "Rejected")
        self.in_milestone(m, "Open")
        self.assertEqual(milestone_work_counts([m], ancestor_rule=True)[m.milestone_id], (1, 1))

    def test_the_agent_counts_leave_it_out_and_report_it_apart(self):
        m = self.milestone()
        self.in_milestone(m, "Closed", due_date=date.today() - timedelta(days=3))
        self.in_milestone(m, "Rejected", due_date=date.today() - timedelta(days=3))
        mine = GET_MY_TASK_SUMMARY.run({}, self.ctx)
        self.assertEqual(mine["status_breakdown"]["Rejected"], 1)
        self.assertEqual(mine["overdue_count"], 0)
        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        row = next(r for r in listed["milestones"] if r["milestone_id"] == m.milestone_id)
        self.assertEqual((row["tasks_total"], row["tasks_closed"], row["overdue_count"]), (1, 1, 0))
        project = GET_PROJECT_SUMMARY.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual(project["status_breakdown"]["Rejected"], 1)
        self.assertEqual(project["overdue_count"], 0)
        self.assertEqual(project["total_tasks"], sum(project["status_breakdown"].values()) - 1)
        team = GET_TEAM_TASK_SUMMARY.run({}, self.ctx)
        self.assertEqual(team["status_breakdown"]["Rejected"], 1)
        self.assertEqual(team["total_tasks"], sum(team["status_breakdown"].values()) - 1)


class AncestorTests(RejectedBase):
    def test_open_children_read_rejected_and_finished_ones_keep_their_own(self):
        root = self.task("Rejected")
        child = self.task("WIP", parent_task_id=root.task_id)
        done = self.task("Closed", parent_task_id=root.task_id)
        grandchild = self.task("Open", parent_task_id=done.task_id)
        self.assertEqual(self.reads(child, done, grandchild), ["Rejected", "Closed", "Closed"])
        self.assertEqual(self.fresh(child).status, "WIP")

    def test_a_rejected_child_stays_rejected_under_a_closed_parent_and_deleted_wins(self):
        closed = self.task("Closed")
        dropped = self.task("Rejected", parent_task_id=closed.task_id)
        deleted = self.task("Deleted")
        under_deleted = self.task("Rejected", parent_task_id=deleted.task_id)
        self.assertEqual(self.reads(dropped, under_deleted), ["Rejected", "Deleted"])


class AutomationTests(RejectedBase):
    def test_rejecting_the_blocker_unblocks_and_a_rejected_task_is_never_reopened(self):
        blocker, blocked = self.task("Open"), self.task("Open")
        TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=blocked, team=self.team, created_by=self.user
        )
        self.assertEqual(self.fresh(blocked).status, "Blocked")
        blocker.status = "Rejected"
        blocker.save()
        self.assertEqual(self.fresh(blocked).status, "Open")
        dropped, other = self.task("Rejected"), self.task("Open")
        TaskDependency.objects.create(
            blocker_task=other, blocked_task=dropped, team=self.team, created_by=self.user
        )
        self.assertEqual(self.fresh(dropped).status, "Rejected")

    def test_a_rejection_is_not_a_close_in_throughput(self):
        task = self.task("Open")
        self.assertEqual(self.put(task, status="Rejected").status_code, 200)
        rows = TaskActivity.objects.filter(
            task=task, action_type=TaskActivityActionType.STATUS, new_value="Rejected"
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(TaskActivity.objects.filter(CLOSE_Q, task=task).count(), 0)

    def test_the_webhook_event_is_task_rejected(self):
        self.assertIn(EVENT_TASK_REJECTED, ALL_EVENTS)
        with self.captureOnCommitCallbacks(execute=True):
            task = self.task("Open")
        with mock.patch.object(webhook_enqueue, "enqueue_event") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                task.status = "Rejected"
                task.save()
        self.assertEqual([c.args[1] for c in enqueue.call_args_list], ["task.rejected"])

    def test_the_calendar_title_is_neither_due_nor_done(self):
        task = SimpleNamespace(title="📅 Ship it", status="Rejected")
        self.assertEqual(calendar_sync._event_title(task), "Ship it")


class WriterAndReaderTests(RejectedBase):
    def test_the_rest_writers_and_the_status_filter_take_it(self):
        self.assertIsNone(status_value_error("Rejected"))
        self.assertEqual(parse_statuses("Rejected,Open"), frozenset({"Rejected", "Open"}))

    def test_the_agent_sets_it(self):
        task = self.task("Open")
        UPDATE_TASK.run(
            {"task_id": task.task_id, "status": "Rejected", "reason": "duplicate"}, self.ctx
        )
        self.assertEqual(self.fresh(task).status, "Rejected")

    def test_csv_words_map_to_it(self):
        for word in ("Rejected", "Won't Fix", "cancelled", "却下", "対応しない"):
            self.assertEqual(task_import.map_status(word), ("Rejected", None), word)
        self.assertEqual(task_import.map_status("x", {"x": "Rejected"}), ("Rejected", None))


class BuiltInRowTests(RejectedBase):
    def test_a_new_list_has_it_last_and_as_a_default_that_can_be_turned_off(self):
        ensure_project_statuses(self.project)
        rows = list(ProjectStatus.objects.filter(project=self.project).order_by("sort_order"))
        last = rows[-1]
        self.assertEqual(
            (last.name, last.category, last.builtin_key, last.is_default),
            ("Rejected", "Rejected", "Rejected", True),
        )
        response = self.client.patch(
            self.url(status_id=last.status_id), {"archived": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_backfill_adds_it_once_to_lists_written_before_it(self):
        ensure_project_statuses(self.project)
        ProjectStatus.objects.filter(project=self.project, builtin_key="Rejected").delete()
        untouched = self.make_project("Never opened", "NO")
        backfill.add_rejected(apps, None)
        backfill.add_rejected(apps, None)
        row = ProjectStatus.objects.get(project=self.project, builtin_key="Rejected")
        self.assertEqual((row.sort_order, row.is_default), (6, True))
        self.assertEqual(str(row.team_id), str(self.team.team_id))
        self.assertFalse(ProjectStatus.objects.filter(project=untouched).exists())

"""MIP-1478: the closed-ancestor rule in five more in-app tools, behind AGENT_CLOSED_ANCESTOR_RULE.

`get_project_summary` and `get_team_task_summary` (counts by status and overdue) read each task by its
EFFECTIVE status, as `get_my_task_summary` does; `get_workload_distribution`, `get_stale_tasks` and
`get_my_schedule` leave out a task that reads as Closed or Deleted, as `get_my_focus_tasks` does.

Not covered, on purpose (a question for the maintainer, in the PR): `get_task_throughput_stats` (counts
close EVENTS: a task closed by an ancestor was never closed at a time) and
`get_milestone_assignee_counts` (a milestone has no parent: the rule never applies to it).
`get_my_blockers` / `get_task_blockers` and the dependency auto-Blocked flip are MIP-1544
(`test_blockers_closed_ancestor`).

Flag off every result is what it was: the fingerprint lines `result.b.<tool>` (`agent_fingerprint`) hold
that, and the first test of each class compares the result with the flag off to the same call made
before any flag existed (the tool's own code path, `rule is None`).
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY, ancestor_rule
from origin.services.task_ancestry import effective_statuses
from origin.tests.test_agent_closed_ancestor_rule import TODAY, Tree, frozen, on

STALE = timezone.now() - timedelta(days=90)


class MoreTools(Tree):
    def on_off(self, name, **args):
        before = self.run_tool(name, **args)
        with on():
            after = self.run_tool(name, **args)
        return before, after

    def stale(self):
        TaskMaster.objects.filter(team=self.team).update(ts_updated_at=STALE)


class CountsTests(MoreTools):
    def test_project_summary_reads_the_effective_status(self):
        off, on_ = self.on_off("get_project_summary", project_id=self.project.project_id)
        # C1 (WIP), G1, M2, MC1 (Open) read as Closed; DC (WIP) and M3 (Open) read as Deleted: still in
        # the total, in no bucket (a task whose own status is Deleted is the same)
        self.assertEqual(off["status_breakdown"]["Closed"] + 4, on_["status_breakdown"]["Closed"])
        self.assertEqual(off["status_breakdown"]["WIP"] - 2, on_["status_breakdown"]["WIP"])
        self.assertEqual(off["status_breakdown"]["Open"] - 4, on_["status_breakdown"]["Open"])
        self.assertEqual(off["total_tasks"], on_["total_tasks"])
        # the overdue one under the Closed parent (G1) is no longer overdue; the plain ones stay
        self.assertEqual(off["overdue_count"] - 1, on_["overdue_count"])
        self.assertEqual(
            {
                k: v
                for k, v in on_.items()
                if k not in ("status_breakdown", "overdue_count", "__summary__")
            },
            {
                k: v
                for k, v in off.items()
                if k not in ("status_breakdown", "overdue_count", "__summary__")
            },
        )

    def test_project_summary_equals_the_walkers_own_counts(self):
        _, on_ = self.on_off("get_project_summary", project_id=self.project.project_id)
        # the walker's own reading (`task_ancestry`), task by task: the rule's definition
        tasks = list(
            TaskMaster.objects.filter(project=self.project, is_init_task=False, is_deleted=False)
        )
        found = effective_statuses([t.task_id for t in tasks], self.team.team_id)
        counts = {s: 0 for s in ("Open", "WIP", "Blocked", "Pending", "Closed")}
        overdue = 0
        for t in tasks:
            reads = found[t.task_id].status
            if reads in counts:
                counts[reads] += 1
            if reads not in ("Closed", "Deleted") and t.due_date and t.due_date < TODAY:
                overdue += 1
        self.assertEqual(on_["status_breakdown"], counts)
        self.assertEqual(on_["overdue_count"], overdue)

    def test_team_task_summary_reads_the_effective_status(self):
        off, on_ = self.on_off("get_team_task_summary")
        self.assertEqual(off["status_breakdown"]["Closed"] + 4, on_["status_breakdown"]["Closed"])
        self.assertEqual(off["status_breakdown"]["Deleted"] + 2, on_["status_breakdown"]["Deleted"])
        self.assertEqual(off["total_tasks"], on_["total_tasks"])
        self.assertEqual(off["overdue_count"] - 1, on_["overdue_count"])
        (p_off,), (p_on,) = off["per_project"], on_["per_project"]
        self.assertEqual(p_off["total"], p_on["total"])
        self.assertEqual(p_off["overdue"] - 1, p_on["overdue"])
        self.assertLess(p_on["open"], p_off["open"])

    def test_the_status_itself_is_never_changed(self):
        with on():
            self.run_tool("get_project_summary", project_id=self.project.project_id)
            self.run_tool("get_team_task_summary")
        self.t["G1"].refresh_from_db()
        self.assertEqual(self.t["G1"].status, "Open")

    def test_reopening_the_parent_brings_the_branch_back(self):
        self.set(self.t["P"], "Open")
        with on():
            on_ = self.run_tool("get_project_summary", project_id=self.project.project_id)
        off = self.run_tool("get_project_summary", project_id=self.project.project_id)
        # only the task in the Closed milestone (MC1) still reads as Closed
        self.assertEqual(on_["status_breakdown"]["Closed"], off["status_breakdown"]["Closed"] + 1)

    def test_one_extra_query_per_call(self):
        from django.db import connection  # noqa: PLC0415
        from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

        for name, args in (
            ("get_project_summary", {"project_id": self.project.project_id}),
            ("get_team_task_summary", {}),
        ):
            with frozen(), CaptureQueriesContext(connection) as before:
                REGISTRY[name].run(args, self.ctx)
            with on(), frozen(), CaptureQueriesContext(connection) as after:
                REGISTRY[name].run(args, self.ctx)
            self.assertEqual(len(after) - len(before), 1, name)


class LeftOutTests(MoreTools):
    def test_workload_distribution_leaves_out_what_reads_as_closed(self):
        off, on_ = self.on_off("get_workload_distribution")

        def active(result):
            (row,) = result["assignees"]
            return (
                row["open_count"] + row["wip_count"] + row["blocked_count"] + row["pending_count"]
            )

        # C1, G1, DC, M2, M3 and MC1 read as Closed or Deleted
        self.assertEqual(active(off) - 6, active(on_))
        (row_off,), (row_on,) = off["assignees"], on_["assignees"]
        self.assertEqual(row_off["overdue_count"] - 1, row_on["overdue_count"])  # G1

    def test_stale_tasks_leave_out_what_reads_as_closed(self):
        self.stale()
        off, on_ = self.on_off("get_stale_tasks", stale_days=30, limit=50)
        off_ids = {r["task_id"] for r in off["tasks"]}
        on_ids = {r["task_id"] for r in on_["tasks"]}
        self.assertEqual(off_ids - on_ids, self.ids("C1", "G1", "DC", "M2", "M3", "MC1"))
        self.assertEqual(on_ids - off_ids, set())
        keep = {r["task_id"]: r for r in off["tasks"] if r["task_id"] in on_ids}
        self.assertEqual({r["task_id"]: r for r in on_["tasks"]}, keep)

    def test_my_schedule_leaves_out_what_reads_as_closed(self):
        soon = TODAY + timedelta(days=1)  # `run_tool` freezes the clock at NOW
        TaskMaster.objects.filter(pk__in=[self.t[k].pk for k in ("C1", "A2")]).update(due_date=soon)
        off, on_ = self.on_off("get_my_schedule")
        off_ids = {r["task_id"] for r in off["tasks_due"]}
        on_ids = {r["task_id"] for r in on_["tasks_due"]}
        self.assertEqual(off_ids, self.ids("C1", "A2"))
        self.assertEqual(on_ids, self.ids("A2"))
        self.assertEqual(off["calendar_status"], on_["calendar_status"])

    def test_a_project_the_viewer_is_not_in_is_still_refused(self):
        # the rule reads only the projects the tool already scoped to
        from origin.search_engine.agent.tools.base import ToolForbidden  # noqa: PLC0415

        with on(), self.assertRaises(ToolForbidden):
            REGISTRY["get_workload_distribution"].run({"project_id": 990999}, self.ctx)
        with on(), self.assertRaises(ToolForbidden):
            REGISTRY["get_stale_tasks"].run({"stale_days": 7, "project_id": 990999}, self.ctx)

    def test_one_extra_query_per_call(self):
        from django.db import connection  # noqa: PLC0415
        from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

        for name, args in (
            ("get_workload_distribution", {}),
            ("get_stale_tasks", {"stale_days": 30}),
            ("get_my_schedule", {}),
        ):
            with frozen(), CaptureQueriesContext(connection) as before:
                REGISTRY[name].run(args, self.ctx)
            with on(), frozen(), CaptureQueriesContext(connection) as after:
                REGISTRY[name].run(args, self.ctx)
            self.assertEqual(len(after) - len(before), 1, name)


class DeclarationTests(MoreTools):
    def test_no_declaration_moves_for_these_five_not_even_with_the_flag_on(self):
        """On purpose: a sentence per tool would be paid in every request's prefix (the all-on prefix is
        held at its budget), and the results say what they read (`status_breakdown`). Question in the PR."""
        five = {
            "get_project_summary",
            "get_team_task_summary",
            "get_workload_distribution",
            "get_stale_tasks",
            "get_my_schedule",
        }
        before = {d.name: d for d in _build_tool_declarations()}
        with on():
            after = {d.name: d for d in _build_tool_declarations()}
        for name in five:
            self.assertEqual(after[name], before[name], name)

    def test_the_off_path_runs_no_rule_query(self):
        with mock.patch.object(ancestor_rule, "rule_for_projects") as rule:
            for name, args in (
                ("get_project_summary", {"project_id": self.project.project_id}),
                ("get_team_task_summary", {}),
                ("get_workload_distribution", {}),
                ("get_stale_tasks", {"stale_days": 30}),
                ("get_my_schedule", {}),
            ):
                self.run_tool(name, **args)
        rule.assert_not_called()

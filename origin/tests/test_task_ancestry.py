"""The closed-ancestor rule as one server-side helper (MIP-1413).

Pinned here:

  * the walker: depth > 1, the milestone path, a cycle, a missing parent, Deleted beats
    Closed, which ancestor is named;
  * `effective_status` vs the task's own status (set exactly when the rule changes it);
  * the milestone progress bar still reads the same (it now calls this walker;
    `test_milestone_ancestor_rule` pins its numbers);
  * the MCP reads: `get_task` and `list_tasks` carry the fields, an unreadable ancestor is
    named by id only, the `status` filter is unchanged, and the in-app registry tool's
    result is untouched;
  * two queries however deep or wide the tree is.
"""

from __future__ import annotations

import itertools
import json

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.milestone_service import ensure_backing_task
from origin.services.task_ancestry import (
    MAX_DEPTH,
    Verdict,
    ancestor_verdicts,
    effective,
    effective_statuses,
)
from origin.tests.test_mcp_endpoint import McpBase


def row(task_id, *, parent=None, milestone=None, is_milestone=False, status="Open"):
    return (task_id, milestone, parent, is_milestone, status)


class WalkerTests(SimpleTestCase):
    """The pure walk: no database."""

    def test_any_depth_names_the_nearest_closed_ancestor(self):
        rows = [
            row(1, status="Closed"),
            row(2, parent=1),
            row(3, parent=2, status="WIP"),
            row(4, parent=3),
        ]
        v = ancestor_verdicts(rows)
        self.assertEqual(v[2], Verdict("Closed", 1))
        self.assertEqual(v[4], Verdict("Closed", 1))
        self.assertNotIn(1, v, "a task's own status is never the reason")

    def test_the_nearest_closed_ancestor_wins_over_a_farther_one(self):
        rows = [row(1, status="Closed"), row(2, parent=1, status="Closed"), row(3, parent=2)]
        self.assertEqual(ancestor_verdicts(rows)[3], Verdict("Closed", 2))

    def test_the_milestone_backing_row_is_an_ancestor(self):
        rows = [
            row(10, milestone=7, is_milestone=True, status="Closed"),
            row(11, milestone=7),
            row(12, parent=11, milestone=7),
        ]
        v = ancestor_verdicts(rows)
        self.assertEqual(v[11], Verdict("Closed", 10))
        self.assertEqual(v[12], Verdict("Closed", 10))
        self.assertNotIn(10, v)

    def test_a_milestone_is_not_its_own_ancestor(self):
        rows = [row(10, milestone=7, is_milestone=True, status="Open")]
        self.assertEqual(ancestor_verdicts(rows), {})

    def test_deleted_beats_closed_wherever_each_sits(self):
        deeper_deleted = [
            row(1, status="Deleted"),
            row(2, parent=1, status="Closed"),
            row(3, parent=2),
        ]
        self.assertEqual(ancestor_verdicts(deeper_deleted)[3], Verdict("Deleted", 1))
        milestone_deleted = [
            row(10, milestone=7, is_milestone=True, status="Deleted"),
            row(1, status="Closed", milestone=7),
            row(2, parent=1, milestone=7),
        ]
        self.assertEqual(ancestor_verdicts(milestone_deleted)[2], Verdict("Deleted", 10))

    def test_the_parent_chain_names_the_ancestor_before_the_milestone_row(self):
        rows = [
            row(10, milestone=7, is_milestone=True, status="Closed"),
            row(1, status="Closed"),
            row(2, parent=1, milestone=7),
        ]
        self.assertEqual(ancestor_verdicts(rows)[2], Verdict("Closed", 1))

    def test_above_the_parent_the_parent_link_is_read_before_the_milestone_link(self):
        # Task 4 sits under 3, which has a Closed parent (1) and a Closed milestone row (10).
        # Nearest first applies at every level, not only to the task's own two links.
        rows = [
            row(10, milestone=7, is_milestone=True, status="Closed"),
            row(1, status="Closed"),
            row(3, parent=1, milestone=7),
            row(4, parent=3),
        ]
        self.assertEqual(ancestor_verdicts(rows)[4], Verdict("Closed", 1))

    def test_a_cycle_is_survived(self):
        rows = [row(1, parent=2), row(2, parent=1), row(3, parent=1)]
        self.assertEqual(ancestor_verdicts(rows), {})
        closed_cycle = [row(1, parent=2, status="Closed"), row(2, parent=1), row(3, parent=2)]
        v = ancestor_verdicts(closed_cycle)
        self.assertEqual(v[2], Verdict("Closed", 1))
        self.assertEqual(v[3], Verdict("Closed", 1))

    def test_a_loop_with_a_closed_task_reads_the_same_in_every_row_order(self):
        # MIP-1426. Rows 1 -> 3 -> 5 (Closed) -> 2 -> 1. 1, 2 and 3 are under the Closed
        # task 5; 5 is not (its own status is never the reason). The walker used to
        # memoise a result computed while its loop guard had cut the walk short, so the
        # answer depended on which row came first: four different answers over the 24
        # orders (some missed row 2, some also flagged the Closed row 5 itself).
        rows = [
            row(1, parent=3),
            row(2, parent=1),
            row(3, parent=5),
            row(5, parent=2, status="Closed"),
        ]
        closed_by_5 = Verdict("Closed", 5)
        want = {1: closed_by_5, 2: closed_by_5, 3: closed_by_5}
        for order in itertools.permutations(rows):
            with self.subTest(order=[r[0] for r in order]):
                self.assertEqual(ancestor_verdicts(order), want)

    def test_a_loop_that_holds_a_deleted_task_names_it_in_every_row_order(self):
        # Deleted beats Closed also inside a loop: 4 (Closed) -> 3 -> 2 (Deleted) -> 1 -> 4.
        rows = [
            row(1, parent=4),
            row(2, parent=1, status="Deleted"),
            row(3, parent=2),
            row(4, parent=3, status="Closed"),
        ]
        deleted_2, closed_4 = Verdict("Deleted", 2), Verdict("Closed", 4)
        # Row 2 is the Deleted one: its own status is not the reason, so it reads the Closed 4.
        want = {1: deleted_2, 2: closed_4, 3: deleted_2, 4: deleted_2}
        for order in itertools.permutations(rows):
            with self.subTest(order=[r[0] for r in order]):
                self.assertEqual(ancestor_verdicts(order), want)

    def test_a_task_that_is_its_own_parent_is_not_under_itself(self):
        rows = [row(1, parent=1, status="Closed"), row(2, parent=2)]
        self.assertEqual(ancestor_verdicts(rows), {})

    def test_a_branch_deeper_than_the_cap_is_a_bad_link_and_ends_short(self):
        depth = MAX_DEPTH + 5
        rows = [row(0, status="Closed")] + [row(i, parent=i - 1) for i in range(1, depth + 1)]
        v = ancestor_verdicts(rows)
        self.assertEqual(v[1], Verdict("Closed", 0))
        self.assertEqual(v[MAX_DEPTH], Verdict("Closed", 0))
        self.assertNotIn(MAX_DEPTH + 1, v)
        self.assertNotIn(depth, v)

    def test_a_missing_parent_ends_the_walk(self):
        rows = [row(2, parent=999), row(3, parent=2)]
        self.assertEqual(ancestor_verdicts(rows), {})

    def test_effective_is_set_exactly_when_the_rule_changes_the_reading(self):
        closed = Verdict("Closed", 1)
        deleted = Verdict("Deleted", 1)
        self.assertEqual(effective("Open", None), ("Open", None))
        self.assertEqual(effective("Open", closed), ("Closed", 1))
        self.assertEqual(effective("Blocked", deleted), ("Deleted", 1))
        self.assertEqual(effective("Closed", closed), ("Closed", None))
        self.assertEqual(effective("Closed", deleted), ("Deleted", 1))
        self.assertEqual(effective("Deleted", closed), ("Deleted", None))


class AncestryBase(McpBase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self._n = 100

    def make(self, title, *, parent=None, milestone=None, status="Open", project=None, **kw):
        self._n += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            reporter=kw.pop("reporter", self.user),
            assignee=kw.pop("assignee", self.user),
            title=title,
            status=status,
            project_task_number=self._n,
            parent_task_id=parent.task_id if parent else None,
            milestone=milestone,
            **kw,
        )

    def milestone(self, title, status="Open"):
        m = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title=title, status="Open"
        )
        backing = ensure_backing_task(m)
        self.set(backing, status)
        m.refresh_from_db()
        return m, backing

    def set(self, task, status):
        TaskMaster.objects.filter(pk=task.pk).update(status=status)


class EffectiveStatusesTests(AncestryBase):
    def test_a_task_under_a_closed_parent_in_the_database(self):
        top = self.make("top", status="Closed")
        mid = self.make("mid", parent=top)
        leaf = self.make("leaf", parent=mid, status="WIP")
        alone = self.make("alone")
        got = effective_statuses([mid.task_id, leaf.task_id, alone.task_id], self.team.team_id)
        self.assertEqual(got[mid.task_id], ("Closed", top.task_id))
        self.assertEqual(got[leaf.task_id], ("Closed", top.task_id))
        self.assertEqual(got[alone.task_id], ("Open", None))
        # the status itself is not changed
        self.assertEqual(TaskMaster.objects.get(pk=leaf.pk).status, "WIP")

    def test_a_closed_milestone_closes_its_tasks_and_reopening_restores_them(self):
        m, backing = self.milestone("M", status="Closed")
        child = self.make("child", milestone=m)
        under_parent = self.make("by parent", parent=backing)
        got = effective_statuses([child.task_id, under_parent.task_id], self.team.team_id)
        self.assertEqual(got[child.task_id], ("Closed", backing.task_id))
        self.assertEqual(got[under_parent.task_id], ("Closed", backing.task_id))
        self.set(backing, "Open")
        got = effective_statuses([child.task_id], self.team.team_id)
        self.assertEqual(got[child.task_id], ("Open", None))

    def test_a_soft_deleted_parent_ends_the_walk(self):
        top = self.make("top", status="Closed")
        child = self.make("child", parent=top)
        TaskMaster.objects.filter(pk=top.pk).update(is_deleted=True)
        self.assertEqual(
            effective_statuses([child.task_id], self.team.team_id)[child.task_id],
            ("Open", None),
        )

    def test_another_teams_task_has_no_entry(self):
        task = self.make("mine")
        self.assertEqual(
            effective_statuses([task.task_id], "6f1c1f4e-0000-4000-8000-0000000000aa"), {}
        )

    def test_two_queries_however_deep_and_wide(self):
        def build(depth, width):
            top = self.make("top", status="Closed")
            leaves = []
            for _ in range(width):
                parent = top
                for _ in range(depth):
                    parent = self.make("node", parent=parent)
                leaves.append(parent.task_id)
            return leaves

        small = build(2, 2)
        with CaptureQueriesContext(connection) as few:
            effective_statuses(small, self.team.team_id)
        big = build(8, 6)
        with CaptureQueriesContext(connection) as many:
            got = effective_statuses(big, self.team.team_id)
        self.assertEqual(len(few), 2)
        self.assertEqual(len(many), 2)
        self.assertTrue(all(e.status == "Closed" for e in got.values()))


class McpReadTests(AncestryBase):
    def setUp(self):
        super().setUp()
        self._key()

    def call(self, name, **args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def test_get_task_carries_effective_status_and_the_ancestor(self):
        top = self.make("top", status="Closed")
        child = self.make("child", parent=top)
        data = self.call("get_task", task_id=str(child.task_id))
        self.assertEqual(data["status"], "Open")
        self.assertEqual(data["effective_status"], "Closed")
        self.assertEqual(
            data["closed_by_ancestor"], {"task_id": top.task_id, "display_id": top.display_id}
        )
        # progress is still added: the two augments are chained, not replaced
        self.assertIn("last_progress_at", data)

    def test_a_task_with_no_closed_ancestor_reads_its_own_status(self):
        alone = self.make("alone", status="WIP")
        data = self.call("get_task", task_id=str(alone.task_id))
        self.assertEqual(data["effective_status"], "WIP")
        self.assertNotIn("closed_by_ancestor", data)

    def test_list_tasks_rows_carry_it_and_the_status_filter_is_unchanged(self):
        top = self.make("top", status="Closed")
        child = self.make("child", parent=top)
        alone = self.make("alone")
        data = self.call("list_tasks", project_id=self.project.project_id, status=["Open"])
        rows = {r["task_id"]: r for r in data["tasks"]}
        self.assertIn(child.task_id, rows, "the filter reads the row's own status")
        self.assertEqual(rows[child.task_id]["effective_status"], "Closed")
        self.assertEqual(rows[child.task_id]["closed_by_ancestor"]["task_id"], top.task_id)
        self.assertEqual(rows[alone.task_id]["effective_status"], "Open")
        self.assertIn("last_progress_at", rows[alone.task_id])

    def test_an_ancestor_the_caller_cannot_read_is_named_by_id_only(self):
        """The caller is the task's assignee but not in its project: they can read the task,
        not its parent."""
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Board only",
            code="BRD",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user2)
        top = self.make(
            "Secret parent",
            status="Closed",
            project=other,
            assignee=self.user2,
            reporter=self.user2,
        )
        child = self.make("Mine", parent=top, project=other, assignee=self.user)
        data = self.call("get_task", task_id=str(child.task_id))
        self.assertEqual(data["effective_status"], "Closed")
        self.assertEqual(data["closed_by_ancestor"], {"task_id": top.task_id, "display_id": None})
        self.assertNotIn("Secret parent", json.dumps(data))

    def test_the_in_app_agents_tool_result_is_untouched(self):
        top = self.make("top", status="Closed")
        child = self.make("child", parent=top)
        result = REGISTRY["fetch_task"].run({"task_id": child.task_id}, self.ctx)
        self.assertNotIn("effective_status", result)
        self.assertNotIn("closed_by_ancestor", result)

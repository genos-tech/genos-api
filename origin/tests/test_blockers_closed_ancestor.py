"""MIP-1544: the closed-ancestor rule for blockers, in the two tools AND in the auto-Blocked flip.

A blocker that sits under a Closed or Deleted parent, grandparent or milestone reads as Closed
(`services/task_ancestry.py`), so it no longer holds anything up:

  * the auto-Blocked flip (`services/task_blocking.sync_blocked_status`, product behaviour, NOT behind
    a flag) does not count it, and re-runs for the dependents of every task under an ancestor when that
    ancestor is closed, deleted or reopened (the descendant itself never changes status);
  * `get_my_blockers` and `get_task_blockers` leave such an edge out, behind
    `SEARCH_ENGINE["AGENT_CLOSED_ANCESTOR_RULE"]` (flag off: every result is what it was).

One rule in both places: the board never says Blocked while the tools say nothing blocks it. A blocker
that is genuinely open still blocks, and reopening the ancestor restores both.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import REGISTRY
from origin.services import task_ancestry
from origin.services.task_blocking import sync_blocked_status
from origin.tests.test_agent_closed_ancestor_rule import frozen, on
from origin.tests.test_mcp_shared_flag_effects import CLOSED, engine
from origin.tests.test_task_ancestry import AncestryBase


class BlockerBase(AncestryBase):
    def save_status(self, task, status):
        """A real save, so the signals fire (`AncestryBase.set` uses `.update()` and skips them)."""
        task.refresh_from_db()
        task.status = status
        task.save(update_fields=["status", "ts_updated_at"])

    def status(self, task):
        return TaskMaster.objects.get(pk=task.pk).status

    def dep(self, blocker, blocked):
        return TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=blocked, team=self.team, created_by=self.user
        )

    def milestone(self, title, status="Open"):
        made = super().milestone(title, status)
        self._n += 1000  # the backing row took the next project task number
        return made

    def milestone_closed(self):
        m, backing = self.milestone("Shipped")
        self.save_status(backing, "Closed")
        return m, backing


class FlipTests(BlockerBase):
    def test_a_blocker_under_a_closed_parent_does_not_block(self):
        parent = self.make("closed parent", status="Closed")
        blocker = self.make("blocker", parent=parent, status="WIP")
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Open")
        self.assertEqual(self.status(blocker), "WIP")  # its own status is never changed

    def test_grandparent_deleted_parent_and_closed_milestone_likewise(self):
        top = self.make("closed top", status="Closed")
        mid = self.make("mid", parent=top)
        deep = self.make("deep blocker", parent=mid)
        gone = self.make("deleted parent", status="Deleted")
        under_gone = self.make("under deleted", parent=gone)
        m, _backing = self.milestone_closed()
        in_m = self.make("in the closed milestone", milestone=m)
        for blocker in (deep, under_gone, in_m):
            dependent = self.make("dependent")
            self.dep(blocker, dependent)
            self.assertEqual(self.status(dependent), "Open", blocker.title)

    def test_a_genuinely_open_blocker_still_blocks(self):
        blocker = self.make("plain blocker")
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Blocked")

    def test_one_open_blocker_among_closed_ancestor_ones_keeps_it_blocked(self):
        parent = self.make("closed parent", status="Closed")
        dead = self.make("under closed", parent=parent)
        live = self.make("live")
        dependent = self.make("dependent")
        self.dep(dead, dependent)
        self.assertEqual(self.status(dependent), "Open")
        self.dep(live, dependent)
        self.assertEqual(self.status(dependent), "Blocked")
        self.save_status(live, "Closed")
        self.assertEqual(self.status(dependent), "Open")

    def test_closing_the_parent_unblocks_and_reopening_it_blocks_again(self):
        parent = self.make("parent")
        blocker = self.make("blocker", parent=parent)
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Blocked")
        self.save_status(parent, "Closed")
        self.assertEqual(self.status(dependent), "Open")
        self.save_status(parent, "Open")
        self.assertEqual(self.status(dependent), "Blocked")

    def test_the_same_through_a_grandparent(self):
        top = self.make("top")
        mid = self.make("mid", parent=top)
        blocker = self.make("blocker", parent=mid)
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.save_status(top, "Closed")
        self.assertEqual(self.status(dependent), "Open")
        self.save_status(top, "Open")
        self.assertEqual(self.status(dependent), "Blocked")

    def test_the_same_through_a_milestone(self):
        m, backing = self.milestone("M")
        blocker = self.make("blocker", milestone=m)
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Blocked")
        self.save_status(backing, "Closed")
        self.assertEqual(self.status(dependent), "Open")
        self.save_status(backing, "Open")
        self.assertEqual(self.status(dependent), "Blocked")

    def test_a_deleted_parent_status_unblocks_too(self):
        parent = self.make("parent")
        blocker = self.make("blocker", parent=parent)
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.save_status(parent, "Deleted")
        self.assertEqual(self.status(dependent), "Open")

    def test_reopening_one_parent_does_not_unblock_what_another_open_blocker_holds(self):
        parent = self.make("parent")
        dead = self.make("under parent", parent=parent)
        live = self.make("live")
        dependent = self.make("dependent")
        self.dep(dead, dependent)
        self.dep(live, dependent)
        self.save_status(parent, "Closed")
        self.assertEqual(self.status(dependent), "Blocked")  # `live` still holds it

    def test_a_manual_blocked_task_with_no_blockers_is_never_touched(self):
        parent = self.make("parent")
        self.make("child", parent=parent)
        lone = self.make("lone", status="Blocked")
        self.save_status(parent, "Closed")
        self.save_status(parent, "Open")
        self.assertEqual(self.status(lone), "Blocked")

    def test_a_sync_run_by_hand_reads_the_same_rule(self):
        parent = self.make("closed parent", status="Closed")
        blocker = self.make("blocker", parent=parent)
        dependent = self.make("dependent", status="Blocked")
        TaskDependency.objects.bulk_create(
            [TaskDependency(blocker_task=blocker, blocked_task=dependent, team=self.team)]
        )
        sync_blocked_status([dependent.task_id])
        self.assertEqual(self.status(dependent), "Open")

    def test_a_blocker_with_no_project_has_only_its_own_status(self):
        blocker = TaskMaster.objects.create(
            team=self.team, title="no project", assignee=self.user, reporter=self.user
        )
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Blocked")

    def test_the_status_of_the_ancestors_children_is_never_written(self):
        parent = self.make("parent")
        blocker = self.make("blocker", parent=parent, status="WIP")
        self.dep(blocker, self.make("dependent"))
        self.save_status(parent, "Closed")
        self.assertEqual(self.status(blocker), "WIP")

    def test_closing_a_milestone_the_way_the_app_does(self):
        """The milestone's own status is canonical; `sync_backing_task` mirrors it onto the backing row."""
        from origin.services.milestone_service import sync_backing_task  # noqa: PLC0415

        m, _backing = self.milestone("M")
        blocker = self.make("blocker", milestone=m)
        dependent = self.make("dependent")
        self.dep(blocker, dependent)
        self.assertEqual(self.status(dependent), "Blocked")
        m.status = "Closed"
        m.save()
        sync_backing_task(m)
        self.assertEqual(self.status(dependent), "Open")
        m.status = "Open"
        m.save()
        sync_backing_task(m)
        self.assertEqual(self.status(dependent), "Blocked")

    def test_a_failing_resync_never_breaks_the_save(self):
        parent = self.make("parent")
        self.make("child", parent=parent)
        self.dep(self.make("a"), self.make("b"))
        with mock.patch.object(task_ancestry, "descendant_ids", side_effect=RuntimeError("boom")):
            self.save_status(parent, "Closed")
        self.assertEqual(self.status(parent), "Closed")

    def test_a_close_with_no_dependency_in_the_project_reads_no_tree(self):
        """The cost guard: closing a task in a project with no edge at all walks nothing."""
        parent = self.make("parent")
        self.make("child", parent=parent)
        with mock.patch.object(task_ancestry, "descendant_ids") as walk:
            self.save_status(parent, "Closed")
        walk.assert_not_called()


class MyBlockersTests(BlockerBase):
    def setUp(self):
        super().setUp()
        self.parent = self.make("closed parent", status="Closed")
        self.dead = self.make(
            "blocker under closed", parent=self.parent, status="WIP", assignee=None
        )
        self.live = self.make("live blocker", assignee=None)
        self.mine = self.make("mine, blocked on two")
        self.dep(self.dead, self.mine)
        self.dep(self.live, self.mine)
        self.other = self.make("someone's, waiting on me", assignee=None)
        self.dep(self.mine, self.other)
        self.waiting_on_dead = self.make("waiting on a dead one")
        self.dep(self.dead, self.waiting_on_dead)

    def call(self):
        return self.run_tool()

    def run_tool(self):
        with frozen():
            return REGISTRY["get_my_blockers"].run({}, self.ctx)

    def blocker_ids(self, result):
        return {e["task_id"] for row in result["blocked_on_me"] for e in row["blocked_by"]}

    def test_flag_off_is_what_it_was(self):
        result = self.run_tool()
        self.assertEqual(self.blocker_ids(result), {self.dead.task_id, self.live.task_id})
        self.assertIn(self.waiting_on_dead.task_id, {r["task_id"] for r in result["blocked_on_me"]})

    def test_a_blocker_under_a_closed_parent_is_not_listed(self):
        with on():
            result = self.run_tool()
        self.assertEqual(self.blocker_ids(result), {self.live.task_id})
        # the task whose only blocker is under the closed parent is not blocked at all
        self.assertEqual({r["task_id"] for r in result["blocked_on_me"]}, {self.mine.task_id})
        self.assertEqual(result["blocked_on_me_count"], 1)
        self.assertNotIn(
            "dead", " ".join(e["title"] for r in result["blocked_on_me"] for e in r["blocked_by"])
        )

    def test_blocking_others_leaves_out_a_waiter_that_reads_as_closed(self):
        waiter_parent = self.make("closed", status="Closed")
        waiter = self.make("waiter under closed", parent=waiter_parent)
        self.dep(self.mine, waiter)
        off = self.run_tool()
        with on():
            on_ = self.run_tool()

        def waiting(result):
            return {e["task_id"] for r in result["blocking_others"] for e in r["blocking"]}

        self.assertIn(waiter.task_id, waiting(off))
        self.assertNotIn(waiter.task_id, waiting(on_))
        self.assertIn(self.other.task_id, waiting(on_))

    def test_my_own_task_under_a_closed_ancestor_is_not_open_work(self):
        mine_parent = self.make("closed", status="Closed")
        mine_dead = self.make("mine, under closed", parent=mine_parent)
        self.dep(self.live, mine_dead)
        with on():
            result = self.run_tool()
        self.assertNotIn(mine_dead.task_id, {r["task_id"] for r in result["blocked_on_me"]})
        self.assertIn(mine_dead.task_id, {r["task_id"] for r in self.run_tool()["blocked_on_me"]})

    def test_reopening_the_ancestor_restores_the_edges(self):
        self.save_status(self.parent, "Open")
        with on():
            result = self.run_tool()
        self.assertEqual(self.blocker_ids(result), {self.dead.task_id, self.live.task_id})

    def test_grandparent_and_milestone_likewise(self):
        top = self.make("top", status="Closed")
        mid = self.make("mid", parent=top)
        deep = self.make("deep", parent=mid)
        m, _ = self.milestone_closed()
        in_m = self.make("in closed milestone", milestone=m)
        mine = self.make("mine 2")
        self.dep(deep, mine)
        self.dep(in_m, mine)
        with on():
            result = self.run_tool()
        self.assertNotIn(mine.task_id, {r["task_id"] for r in result["blocked_on_me"]})

    def test_a_genuinely_open_blocker_stays_with_the_flag_on(self):
        with on():
            result = self.run_tool()
        self.assertIn(self.live.task_id, self.blocker_ids(result))
        self.assertIn(self.mine.task_id, {r["task_id"] for r in result["blocking_others"]})

    def test_the_tool_and_the_flip_agree(self):
        """Whatever the tool lists as blocking `mine` is exactly what makes the flip hold it Blocked."""
        with on():
            listed = self.blocker_ids(self.run_tool())
        self.assertEqual(self.status(self.mine), "Blocked")
        self.assertEqual(listed, {self.live.task_id})
        self.assertEqual(self.status(self.waiting_on_dead), "Open")
        self.save_status(self.live, "Closed")
        with on():
            self.assertNotIn(
                self.mine.task_id, {r["task_id"] for r in self.run_tool()["blocked_on_me"]}
            )
        self.assertEqual(self.status(self.mine), "Open")

    def test_one_extra_query(self):
        with frozen(), CaptureQueriesContext(connection) as before:
            REGISTRY["get_my_blockers"].run({}, self.ctx)
        with on(), frozen(), CaptureQueriesContext(connection) as after:
            REGISTRY["get_my_blockers"].run({}, self.ctx)
        self.assertEqual(len(after) - len(before), 1)

    def test_the_off_path_runs_no_rule_query(self):
        from origin.search_engine.agent.tools import ancestor_rule  # noqa: PLC0415

        with mock.patch.object(ancestor_rule, "rule_for_projects") as rule:
            self.run_tool()
        rule.assert_not_called()


class TaskBlockersTests(BlockerBase):
    def setUp(self):
        super().setUp()
        self.parent = self.make("closed parent", status="Closed")
        self.dead = self.make("blocker under closed", parent=self.parent, status="WIP")
        self.live = self.make("live blocker")
        self.target = self.make("target")
        self.dep(self.dead, self.target)
        self.dep(self.live, self.target)
        self.waiter = self.make("waits on target")
        self.dep(self.target, self.waiter)
        self.dead_waiter = self.make("waiter under closed", parent=self.parent)
        self.dep(self.target, self.dead_waiter)

    def run_tool(self, task=None):
        with frozen():
            return REGISTRY["get_task_blockers"].run(
                {"task_id": (task or self.target).task_id}, self.ctx
            )

    def ids(self, rows):
        return {e["task_id"] for e in rows}

    def test_flag_off_is_what_it_was(self):
        result = self.run_tool()
        self.assertEqual(self.ids(result["blocked_by"]), {self.dead.task_id, self.live.task_id})
        self.assertEqual(
            self.ids(result["blocking"]), {self.waiter.task_id, self.dead_waiter.task_id}
        )

    def test_blocked_by_leaves_out_a_blocker_under_a_closed_ancestor(self):
        with on():
            result = self.run_tool()
        self.assertEqual(self.ids(result["blocked_by"]), {self.live.task_id})
        self.assertIn("blocked by 1", result["__summary__"])

    def test_blocking_leaves_out_a_waiter_that_reads_as_closed(self):
        with on():
            result = self.run_tool()
        self.assertEqual(self.ids(result["blocking"]), {self.waiter.task_id})

    def test_reopening_the_ancestor_restores_the_edges(self):
        self.save_status(self.parent, "Open")
        with on():
            result = self.run_tool()
        self.assertEqual(self.ids(result["blocked_by"]), {self.dead.task_id, self.live.task_id})
        self.assertEqual(
            self.ids(result["blocking"]), {self.waiter.task_id, self.dead_waiter.task_id}
        )

    def test_grandparent_and_milestone_likewise(self):
        top = self.make("top", status="Closed")
        mid = self.make("mid", parent=top)
        deep = self.make("deep", parent=mid)
        m, _ = self.milestone_closed()
        in_m = self.make("in closed milestone", milestone=m)
        target = self.make("target 2")
        self.dep(deep, target)
        self.dep(in_m, target)
        with on():
            result = self.run_tool(target)
        self.assertEqual(result["blocked_by"], [])
        self.assertEqual(result["__summary__"].split(": ")[1], "no dependencies")

    def test_the_target_under_a_closed_ancestor_still_answers(self):
        """The question was asked about it by name: its own edges are still read."""
        child = self.make("target under closed", parent=self.parent)
        self.dep(self.live, child)
        with on():
            result = self.run_tool(child)
        self.assertEqual(self.ids(result["blocked_by"]), {self.live.task_id})

    def test_an_edge_into_a_project_the_caller_is_not_in_is_redacted_either_way(self):
        """The caller reads the target as its ASSIGNEE, in a project they are not in: every edge there is
        redacted and counted, whatever the hidden task reads as (the rule never tells what it is)."""
        from origin.models.project.prj_models import ProjectMaster  # noqa: PLC0415

        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Not mine",
            owner=self.user2,
            project_system_user=self.user2,
        )
        top = self.make("hidden top", status="Closed", project=other, assignee=self.user2)
        hidden = self.make("hidden blocker", parent=top, project=other, assignee=self.user2)
        target = self.make("target in a hidden project", project=other)  # assignee: the caller
        waiter = self.make("hidden waiter", parent=top, project=other, assignee=self.user2)
        self.dep(hidden, target)
        self.dep(target, waiter)
        off = self.run_tool(target)
        with on():
            on_ = self.run_tool(target)
        for result in (off, on_):
            self.assertEqual((result["blocked_by"], result["blocking"]), ([], []))
            self.assertEqual(result["redacted_count"], 2)

    def test_an_own_closed_blocker_is_listed_as_before(self):
        done = self.make("done", status="Closed")
        self.dep(done, self.target)
        with on():
            result = self.run_tool()
        self.assertIn(done.task_id, self.ids(result["blocked_by"]))

    def test_a_blocker_that_is_open_still_blocks_with_the_flag_on(self):
        with on():
            result = self.run_tool()
        self.assertEqual(self.status(self.target), "Blocked")
        self.assertTrue(result["blocked_by"])

    def test_one_extra_query(self):
        with frozen(), CaptureQueriesContext(connection) as before:
            REGISTRY["get_task_blockers"].run({"task_id": self.target.task_id}, self.ctx)
        with on(), frozen(), CaptureQueriesContext(connection) as after:
            REGISTRY["get_task_blockers"].run({"task_id": self.target.task_id}, self.ctx)
        self.assertEqual(len(after) - len(before), 1)

    def test_the_off_path_runs_no_rule_query(self):
        from origin.search_engine.agent.tools import ancestor_rule  # noqa: PLC0415

        with mock.patch.object(ancestor_rule, "rule_for_projects") as rule:
            self.run_tool()
        rule.assert_not_called()


class DescendantTests(BlockerBase):
    def test_descendant_ids_follow_the_parent_chain_and_the_milestone(self):
        top = self.make("top")
        mid = self.make("mid", parent=top)
        leaf = self.make("leaf", parent=mid)
        m, backing = self.milestone("M")
        in_m = self.make("in m", milestone=m)
        under_backing = self.make("under backing", parent=backing)
        other = self.make("other")
        self.assertEqual(
            task_ancestry.descendant_ids(top.task_id, self.project.project_id),
            {mid.task_id, leaf.task_id},
        )
        self.assertEqual(
            task_ancestry.descendant_ids(backing.task_id, self.project.project_id),
            {in_m.task_id, under_backing.task_id},
        )
        self.assertEqual(
            task_ancestry.descendant_ids(other.task_id, self.project.project_id), set()
        )

    def test_a_loop_in_the_links_ends(self):
        a = self.make("a")
        b = self.make("b", parent=a)
        TaskMaster.objects.filter(pk=a.pk).update(parent_task_id=b.task_id)
        self.assertEqual(
            task_ancestry.descendant_ids(a.task_id, self.project.project_id), {b.task_id}
        )


class McpTests(BlockerBase):
    """The MCP tools are the same functions: the flag reaches a connected client as well."""

    def setUp(self):
        super().setUp()
        self._key(team=self.team)
        self.parent = self.make("closed parent", status="Closed")
        self.dead = self.make("dead blocker", parent=self.parent, assignee=None)
        self.live = self.make("live blocker", assignee=None)
        self.mine = self.make("mine")
        self.dep(self.dead, self.mine)
        self.dep(self.live, self.mine)

    def payload(self, name, args):
        return self._result(self._call(name, args))["structuredContent"]

    def test_get_task_blockers(self):
        args = {"task_id": self.mine.task_id}
        with engine(**{CLOSED: False}):
            off = self.payload("get_task_blockers", args)
        with engine(**{CLOSED: True}):
            on_ = self.payload("get_task_blockers", args)
        self.assertEqual({e["task_id"] for e in off["blocked_by"]}, self.ids("dead", "live"))
        self.assertEqual({e["task_id"] for e in on_["blocked_by"]}, self.ids("live"))

    def test_get_my_blockers(self):
        with engine(**{CLOSED: False}):
            off = self.payload("get_my_blockers", {})
        with engine(**{CLOSED: True}):
            on_ = self.payload("get_my_blockers", {})
        (row_off,) = off["blocked_on_me"]
        (row_on,) = on_["blocked_on_me"]
        self.assertEqual({e["task_id"] for e in row_off["blocked_by"]}, self.ids("dead", "live"))
        self.assertEqual({e["task_id"] for e in row_on["blocked_by"]}, self.ids("live"))

    def ids(self, *names):
        return {getattr(self, n).task_id for n in names}

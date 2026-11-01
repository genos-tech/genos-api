"""`get_milestone_summary` says where to start (MIP-782).

It returned counts only. A milestone planned with 20 tasks and 18 blocker
edges gave a reader nothing to start from, and "Open" did not mean ready: a
sub-task of a blocked parent shows Open. Now it lists the ready-now tasks
(every blocker closed, the parent's included), the blocked ones with their
open blockers, what is in progress, and the critical path.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class MilestoneOrderOfWorkTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Migration",
            code="MIG",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        self.backing = ensure_backing_task(self.milestone)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, title, *, parent=None, project=None, milestone=True, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            reporter=self.user,
            title=title,
            milestone=self.milestone if milestone else None,
            parent_task_id=(parent.task_id if parent else None),
            **kwargs,
        )

    def _block(self, blocker, blocked):
        return TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=blocked, team=self.team, created_by=self.user
        )

    def _summary(self):
        return GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)

    @staticmethod
    def _titles(rows):
        return [r["title"] for r in rows]

    def test_a_chain_has_one_ready_task_and_a_critical_path_in_order(self):
        a, b, c = self._task("a"), self._task("b"), self._task("c")
        self._block(a, b)
        self._block(b, c)
        out = self._summary()
        self.assertEqual(self._titles(out["ready_now"]), ["a"])
        self.assertEqual(self._titles(out["blocked"]), ["b", "c"])
        self.assertEqual(self._titles(out["critical_path"]), ["a", "b", "c"])
        self.assertEqual(out["ready_now_count"], 1)
        self.assertEqual(out["blocked_count"], 2)
        self.assertNotIn("dependency_cycle", out)
        self.assertIn("1 ready to start, 2 blocked", out["__summary__"])

    def test_a_blocked_task_names_its_open_blockers(self):
        a, d, b = self._task("a"), self._task("d"), self._task("b")
        self._block(a, b)
        self._block(d, b)
        (row,) = self._summary()["blocked"]
        self.assertEqual({x["title"] for x in row["open_blockers"]}, {"a", "d"})
        self.assertEqual(
            {x["display_id"] for x in row["open_blockers"]},
            {a.display_id, d.display_id},
        )

    def test_a_closed_blocker_no_longer_blocks(self):
        a, b = self._task("a", status="Closed"), self._task("b")
        self._block(a, b)
        out = self._summary()
        self.assertEqual(self._titles(out["ready_now"]), ["b"])
        self.assertEqual(out["blocked"], [])
        self.assertEqual(out["critical_path"], [])

    def test_the_blockers_of_a_parent_block_its_subtasks(self):
        a = self._task("a")
        parent = self._task("parent")
        child = self._task("child", parent=parent, status="Open")
        self._block(a, parent)
        out = self._summary()
        # The child shows Open and has no edge of its own, but it is not ready.
        self.assertEqual(TaskMaster.objects.get(pk=child.pk).status, "Open")
        self.assertEqual(self._titles(out["ready_now"]), ["a"])
        blocked = {r["title"]: r for r in out["blocked"]}
        self.assertEqual(set(blocked), {"parent", "child"})
        (via,) = blocked["child"]["open_blockers"]
        self.assertEqual(via["title"], "a")
        self.assertEqual(via["inherited_from"]["title"], "parent")
        self.assertNotIn("inherited_from", blocked["parent"]["open_blockers"][0])
        # a -> parent and a -> child tie for the longest chain; either is
        # one, and it starts with the task to take first.
        self.assertEqual(len(out["critical_path"]), 2)
        self.assertEqual(out["critical_path"][0]["title"], "a")

    def test_a_closed_parents_blockers_do_not_block_the_subtasks(self):
        a = self._task("a")
        parent = self._task("parent", status="Closed")
        self._task("child", parent=parent)
        self._block(a, parent)
        self.assertEqual(set(self._titles(self._summary()["ready_now"])), {"a", "child"})

    def test_the_milestones_own_blockers_block_all_of_its_tasks(self):
        outside = self._task("outside", milestone=False)
        self._task("t1", parent=self.backing)
        self._block(outside, self.backing)
        out = self._summary()
        self.assertEqual(out["ready_now"], [])
        self.assertEqual([r["title"] for r in out["blocked"]], ["t1"])

    def test_work_in_flight_is_listed_apart_and_not_offered_to_start(self):
        self._task("w", status="WIP")
        self._task("o")
        out = self._summary()
        self.assertEqual(self._titles(out["in_progress"]), ["w"])
        self.assertEqual(self._titles(out["ready_now"]), ["o"])

    def test_blocked_by_hand_is_blocked_with_a_note(self):
        self._task("manual", status="Blocked")
        (row,) = self._summary()["blocked"]
        self.assertEqual(row["open_blockers"], [])
        self.assertIn("by hand", row["note"])

    def test_the_longest_chain_wins_and_parallel_work_is_all_ready(self):
        a, b, c, d, e = (self._task(n) for n in "abcde")
        self._block(a, b)
        self._block(b, c)
        self._block(d, c)  # a shorter way into c
        out = self._summary()
        self.assertEqual(set(self._titles(out["ready_now"])), {"a", "d", "e"})
        self.assertEqual(self._titles(out["critical_path"]), ["a", "b", "c"])

    def test_a_blocker_in_a_project_the_caller_cannot_see_is_counted_not_named(self):
        other_owner = self.user2
        hidden_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=other_owner,
            project_system_user=other_owner,
        )
        hidden = self._task("hidden", project=hidden_project, milestone=False)
        b = self._task("b")
        self._block(hidden, b)
        (row,) = self._summary()["blocked"]
        self.assertEqual(row["title"], "b")
        self.assertEqual(row["open_blockers"], [])
        self.assertEqual(row["redacted_blockers"], 1)
        self.assertNotIn("hidden", str(row))

    def test_a_dependency_cycle_is_flagged_and_does_not_hang(self):
        u, v = self._task("u"), self._task("v")
        free = self._task("free")
        self._block(u, v)
        self._block(v, u)
        out = self._summary()
        self.assertTrue(out["dependency_cycle"])
        self.assertEqual(self._titles(out["ready_now"]), ["free"])
        self.assertEqual({r["title"] for r in out["blocked"]}, {"u", "v"})
        self.assertIsNotNone(free)

    def test_the_milestones_own_row_and_closed_tasks_are_not_work(self):
        self._task("done", status="Closed")
        self._task("deleted", status="Deleted")
        out = self._summary()
        self.assertEqual(out["ready_now"], [])
        self.assertEqual(out["in_progress"], [])
        self.assertEqual(out["blocked"], [])

    def test_long_lists_are_capped_with_complete_counts(self):
        for i in range(55):
            self._task(f"t{i}")
        out = self._summary()
        self.assertEqual(len(out["ready_now"]), 50)
        self.assertEqual(out["ready_now_count"], 55)

    def test_the_existing_counts_are_unchanged(self):
        self._task("a")
        self._task("b", status="Closed")
        out = self._summary()
        self.assertEqual(out["total_tasks"], 2 + 1)  # plus the milestone's backing row
        self.assertEqual(out["status_breakdown"]["Closed"], 1)

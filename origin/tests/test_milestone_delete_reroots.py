"""Deleting a milestone re-roots the tasks it detaches (MIP-931).

A task in a milestone hangs off the milestone's backing row and, by the same
bridge, carries that row as its `root_task_id`; everything below it inherits
the same root when it is created. A milestone delete soft-deletes the backing
row and detaches the tasks (the top ones lose their `parent_task_id`), but it
used to leave `root_task_id` alone, so every detached task still pointed at a
deleted row. The task diagram, the sub-task drawer and every new sub-task
(which copies its parent's root) anchor on that column.

After the delete each task that hung directly off the backing row is its own
root, everything below it points at it, and the rows reach the browsers that
sync incrementally (`getProjectTasks?since=`): a bare queryset `.update()`
skips `auto_now`, so the re-rooted rows are stamped by hand (MIP-936).

Both entry points are covered: the UI's DELETE and the agent's tool (which the
MCP endpoint serves) share `services/milestone_manage.delete_milestone`.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.delete_milestone import DELETE_MILESTONE
from origin.services.milestone_manage import reroot_detached_tasks

from .test_milestone_delete_reaches_browsers import BrowserSyncBase

SERVICE = "origin.services.milestone_manage"


def _root(task) -> int | None:
    return TaskMaster.objects.values_list("root_task_id", flat=True).get(pk=task.pk)


class RerootBase(BrowserSyncBase):
    """milestone > top1 > sub1 > subsub1, and a second top with a sibling
    sub-tree (top2 > sub2a > subsub2a, top2 > sub2b); a task in ANOTHER
    milestone (other_top > other_sub); all built the way the product builds
    them (the create signal derives each root, so every task in the milestone
    starts out rooted at its backing row)."""

    def setUp(self):
        super().setUp()
        self.m = self._milestone("Doomed", sprint=self.sprint)
        backing = self.m.task_id

        def make(title, parent_task_id):
            return self._task(
                title,
                milestone=self.m,
                parent_task_id=parent_task_id,
                sprint=self.sprint,
            )

        self.top1 = make("top1", backing)
        self.sub1 = make("sub1", self.top1.task_id)
        self.subsub1 = make("subsub1", self.sub1.task_id)
        self.top2 = make("top2", backing)
        self.sub2a = make("sub2a", self.top2.task_id)
        self.subsub2a = make("subsub2a", self.sub2a.task_id)
        self.sub2b = make("sub2b", self.top2.task_id)

        self.other = self._milestone("Stays", sprint=self.sprint)
        self.other_top = self._task(
            "other top", milestone=self.other, parent_task_id=self.other.task_id
        )
        self.other_sub = self._task(
            "other sub", milestone=self.other, parent_task_id=self.other_top.task_id
        )

        self.tree = [
            self.top1,
            self.sub1,
            self.subsub1,
            self.top2,
            self.sub2a,
            self.subsub2a,
            self.sub2b,
        ]
        self.expected_root = {
            self.top1: self.top1,
            self.sub1: self.top1,
            self.subsub1: self.top1,
            self.top2: self.top2,
            self.sub2a: self.top2,
            self.subsub2a: self.top2,
            self.sub2b: self.top2,
        }
        self.elsewhere = [self.other.task, self.other_top, self.other_sub]

    def _age_everything(self):
        """Every task as a browser saw it long ago: a queryset update is the
        only write that leaves `ts_updated_at` alone. Returns that old stamp."""
        old = timezone.now() - timedelta(days=3)
        TaskMaster.objects.filter(project=self.project).update(ts_updated_at=old)
        return old

    def _delete(self, milestone):  # overridden per entry point
        raise NotImplementedError


class RerootAssertions:
    """The behaviour, asserted the same way whichever door the delete came in by."""

    def test_the_fixture_starts_with_everything_rooted_at_the_backing_row(self):
        # The state the bug leaves behind, so the tests below prove a change.
        for task in self.tree:
            self.assertEqual(_root(task), self.m.task_id, task.title)

    def test_each_top_task_is_its_own_root_and_its_subtree_points_at_it(self):
        self._delete(self.m)

        for task, root in self.expected_root.items():
            self.assertEqual(_root(task), root.task_id, task.title)
        for top in (self.top1, self.top2):
            self.assertEqual(_root(top), top.task_id)
            self.assertIsNone(TaskMaster.objects.get(pk=top.pk).parent_task_id)

    def test_the_tree_inside_each_subtree_is_not_flattened(self):
        self._delete(self.m)

        parents = {t.title: TaskMaster.objects.get(pk=t.pk).parent_task_id for t in self.tree}
        self.assertEqual(parents["sub1"], self.top1.task_id)
        self.assertEqual(parents["subsub1"], self.sub1.task_id)
        self.assertEqual(parents["sub2a"], self.top2.task_id)
        self.assertEqual(parents["subsub2a"], self.sub2a.task_id)
        self.assertEqual(parents["sub2b"], self.top2.task_id)

    def test_another_milestones_tasks_are_untouched(self):
        old = self._age_everything()
        before = {
            t.pk: TaskMaster.objects.filter(pk=t.pk)
            .values("root_task_id", "parent_task_id", "milestone_id", "sprint_id", "is_deleted")
            .get()
            for t in self.elsewhere
        }
        self.assertEqual(_root(self.other_sub), self.other.task_id)

        self._delete(self.m)

        for t in self.elsewhere:
            row = TaskMaster.objects.get(pk=t.pk)
            self.assertEqual(
                {
                    "root_task_id": row.root_task_id,
                    "parent_task_id": row.parent_task_id,
                    "milestone_id": row.milestone_id,
                    "sprint_id": row.sprint_id,
                    "is_deleted": row.is_deleted,
                },
                before[t.pk],
                t.title,
            )
            self.assertEqual(row.ts_updated_at, old, t.title)

    def test_the_deleted_backing_row_keeps_its_own_root(self):
        backing = self.m.task_id
        self._delete(self.m)

        row = TaskMaster.objects.get(task_id=backing)
        self.assertTrue(row.is_deleted)
        self.assertEqual(row.root_task_id, backing)

    def test_every_rerooted_row_gets_a_fresh_stamp(self):
        old = self._age_everything()
        checkpoint = timezone.now()

        self._delete(self.m)

        for task in self.tree:
            self.assertGreater(
                TaskMaster.objects.get(pk=task.pk).ts_updated_at, checkpoint, task.title
            )
        # A row the delete had no reason to touch keeps the stamp it had.
        self.assertEqual(TaskMaster.objects.get(pk=self.other_top.pk).ts_updated_at, old)

    def test_a_chain_deeper_than_the_usual_cap_is_followed_to_the_bottom(self):
        # Fifteen levels below the backing row (the walk that finds descendants
        # is capped at 10 elsewhere), the bottom ones created FIRST so they are
        # met before the rows above them, and every root stale so only the
        # parent chain can say where they belong.
        chain = [
            self._task(f"deep{i}", milestone=self.m, sprint=self.sprint) for i in range(12)
        ]  # deep0 is the bottom, deep11 hangs under subsub1
        for i, task in enumerate(chain):
            above = chain[i + 1].task_id if i + 1 < len(chain) else self.subsub1.task_id
            TaskMaster.objects.filter(pk=task.pk).update(
                parent_task_id=above, root_task_id=task.task_id
            )

        self._delete(self.m)

        for task in chain:
            self.assertEqual(_root(task), self.top1.task_id, task.title)

    def _filed_elsewhere_under_the_tree(self):
        """Two rows in the OTHER milestone, one hanging under `sub1` (the picker
        takes an explicit parent and roots the row at its own milestone), and a
        child of that one."""
        hung = self._task("hung", milestone=self.other, parent_task_id=self.sub1.task_id)
        hung_child = self._task("hung child", milestone=self.other, parent_task_id=hung.task_id)
        TaskMaster.objects.filter(pk__in=[hung.pk, hung_child.pk]).update(
            root_task_id=self.other.task_id
        )
        return hung, hung_child

    def test_a_task_of_another_milestone_hanging_under_the_tree_is_left_exactly_as_it_is(self):
        hung, hung_child = self._filed_elsewhere_under_the_tree()
        old = self._age_everything()
        before = {
            t.pk: TaskMaster.objects.filter(pk=t.pk)
            .values("root_task_id", "parent_task_id", "milestone_id", "sprint_id", "is_deleted")
            .get()
            for t in (hung, hung_child)
        }

        self._delete(self.m)

        for t in (hung, hung_child):
            row = TaskMaster.objects.get(pk=t.pk)
            self.assertEqual(row.root_task_id, self.other.task_id, t.title)
            self.assertEqual(row.milestone_id, self.other.milestone_id, t.title)
            self.assertEqual({k: getattr(row, k) for k in before[t.pk]}, before[t.pk], t.title)
            self.assertEqual(row.ts_updated_at, old, t.title)
        # The tree it hangs under is re-rooted all the same.
        self.assertEqual(_root(self.sub1), self.top1.task_id)

    def test_such_a_row_still_rooted_at_the_dead_backing_row_is_rooted_by_its_chain(self):
        hung, _ = self._filed_elsewhere_under_the_tree()
        TaskMaster.objects.filter(pk=hung.pk).update(root_task_id=self.m.task_id)

        self._delete(self.m)

        self.assertEqual(_root(hung), self.top1.task_id)

    def test_a_sub_task_only_the_reroot_touches_is_stamped_too(self):
        # Filed under the tree by `parent_task_id` alone, citing no milestone:
        # the detach leaves this row out, so only the re-root can reach it.
        hanger = self._task("hanger", parent_task_id=self.sub1.task_id)
        self.assertEqual(_root(hanger), self.m.task_id)
        self.assertIsNone(hanger.milestone_id)
        self._age_everything()
        checkpoint = timezone.now()

        self._delete(self.m)

        self.assertEqual(_root(hanger), self.top1.task_id)
        self.assertGreater(TaskMaster.objects.get(pk=hanger.pk).ts_updated_at, checkpoint)
        self.assertEqual(TaskMaster.objects.get(pk=hanger.pk).parent_task_id, self.sub1.task_id)

    def test_the_history_of_the_detach_says_milestone_delete(self):
        self._delete(self.m)

        for task in self.tree:
            rows = TaskActivity.objects.filter(task=task).exclude(action_type="created")
            self.assertTrue(rows.exists(), task.title)
            self.assertEqual(
                {a.metadata.get("cascade") for a in rows}, {"milestone_delete"}, task.title
            )

    def test_the_browsers_hear_the_new_roots_through_the_delta_endpoint(self):
        held = self._load()  # the browser's last full load
        for task in self.tree:
            self.assertEqual(held[str(task.task_id)]["rootTaskId"], self.m.task_id)
        self._age_everything()
        checkpoint = timezone.now()

        self._delete(self.m)

        delta = self._load(checkpoint)
        for task, root in self.expected_root.items():
            self.assertIn(str(task.task_id), delta, task.title)
            self.assertEqual(delta[str(task.task_id)]["rootTaskId"], root.task_id, task.title)
        # And a full load agrees with the delta.
        full = self._load()
        for task, root in self.expected_root.items():
            self.assertEqual(full[str(task.task_id)]["rootTaskId"], root.task_id, task.title)
        # The other milestone's rows did not change, so they are not re-sent.
        for task in (self.other_top, self.other_sub):
            self.assertNotIn(str(task.task_id), delta)

    # -- rows the product did not build this way ---------------------------

    def test_a_legacy_row_with_no_parent_becomes_its_own_root(self):
        loose = self._task("loose", milestone=self.m, sprint=self.sprint)
        TaskMaster.objects.filter(pk=loose.pk).update(root_task_id=self.m.task_id)

        self._delete(self.m)

        self.assertEqual(_root(loose), loose.task_id)

    def test_a_row_still_rooted_here_but_filed_under_a_task_elsewhere_roots_there(self):
        elsewhere = self._task("elsewhere")  # top-level, in no milestone
        stray = self._task("stray", milestone=self.m, parent_task_id=elsewhere.task_id)
        TaskMaster.objects.filter(pk=stray.pk).update(root_task_id=self.m.task_id)

        self._delete(self.m)

        self.assertEqual(_root(stray), elsewhere.task_id)
        self.assertEqual(TaskMaster.objects.get(pk=stray.pk).parent_task_id, elsewhere.task_id)
        self.assertEqual(_root(elsewhere), elsewhere.task_id)

    def test_a_descendant_with_a_stale_root_is_pointed_at_its_top_by_the_parent_chain(self):
        # The chain decides, not whatever the column held.
        TaskMaster.objects.filter(pk=self.sub2b.pk).update(root_task_id=self.sub2b.task_id)
        TaskMaster.objects.filter(pk=self.subsub1.pk).update(root_task_id=987654321)

        self._delete(self.m)

        self.assertEqual(_root(self.sub2b), self.top2.task_id)
        self.assertEqual(_root(self.subsub1), self.top1.task_id)


class RerootThroughTheUiDeleteTests(RerootAssertions, RerootBase):
    def _delete(self, milestone):
        with (
            mock.patch(f"{SERVICE}.purge_milestone"),
            mock.patch(f"{SERVICE}.purge_task"),
        ):
            res = self.client.delete(f"/api/v2/milestone/{milestone.milestone_id}/")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))


class RerootThroughTheAgentToolTests(RerootAssertions, RerootBase):
    def _delete(self, milestone):
        with (
            mock.patch(f"{SERVICE}.purge_milestone"),
            mock.patch(f"{SERVICE}.purge_task"),
        ):
            out = DELETE_MILESTONE.run({"milestone_id": milestone.milestone_id}, self.ctx)
        self.assertTrue(out["deleted"])


class RerootCostTests(RerootBase):
    """The re-root is a fixed handful of queries however many top tasks there
    are. (The history rows the detach writes are per task and are not part of
    this: they were there before.)"""

    def _queries_to_reroot(self, milestone) -> int:
        with CaptureQueriesContext(connection) as ctx:
            reroot_detached_tasks(milestone.task_id, milestone.milestone_id)
        return len(ctx)

    def test_the_cost_does_not_grow_with_the_number_of_tasks(self):
        small = self._queries_to_reroot(self.m)  # 2 tops, 3 levels, 7 tasks

        bigger = self._milestone("Bigger", sprint=self.sprint)
        for i in range(12):
            top = self._task(f"t{i}", milestone=bigger, parent_task_id=bigger.task_id)
            sub = self._task(f"s{i}", milestone=bigger, parent_task_id=top.task_id)
            self._task(f"ss{i}", milestone=bigger, parent_task_id=sub.task_id)
        large = self._queries_to_reroot(bigger)  # 12 tops, same depth, 36 tasks

        self.assertEqual(large, small)
        # One query per level (and one that finds the bottom), one for the
        # rows, one write.
        self.assertEqual(small, 4 + 1 + 1)
        # Every one of them really was re-rooted (the equality is not two no-ops).
        for i in range(12):
            top = TaskMaster.objects.get(title=f"t{i}", project=self.project)
            self.assertEqual(top.root_task_id, top.task_id)
            for title in (f"s{i}", f"ss{i}"):
                row = TaskMaster.objects.get(title=title, project=self.project)
                self.assertEqual(row.root_task_id, top.task_id)

    def test_a_second_run_finds_nothing_to_write(self):
        reroot_detached_tasks(self.m.task_id, self.m.milestone_id)
        with CaptureQueriesContext(connection) as ctx:
            changed = reroot_detached_tasks(self.m.task_id, self.m.milestone_id)
        self.assertEqual(changed, 0)
        self.assertEqual(len(ctx), 4 + 1)  # reads only

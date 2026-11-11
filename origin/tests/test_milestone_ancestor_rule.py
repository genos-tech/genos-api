"""A Closed or Deleted ancestor takes its whole branch with it (MIP-1389).

Users do not update every status, so the milestone progress the UI shows (the
sidebar chip, the board card, the dashboard, the picker) reads a task under a
Closed ancestor as closed, and one under a Deleted ancestor as not there, the
same rule the task table and board apply on the client. The ancestors are the
`parent_task_id` chain and the backing row of the task's milestone, so a Closed
milestone reads N/N.

The agent/MCP rollups (`milestone_work_q`) are model-visible and keep their old
reading until the December eval: `milestone_work_counts` without the flag is
that reading, and stays pinned by `test_milestone_list_queries`.
"""

import random

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_rollup import milestone_work_counts
from origin.views.task.milestone_views import _serialize_milestone

from .test_milestone_list_queries import MilestoneListBase


class MilestoneAncestorRuleTests(MilestoneListBase):
    def _counts(self, m):
        return milestone_work_counts([m], ancestor_rule=True)[m.milestone_id]

    def _set(self, task, status):
        TaskMaster.objects.filter(pk=task.pk).update(status=status)

    def test_an_open_task_under_a_closed_parent_counts_as_closed_at_any_depth(self):
        m = self._milestone("M")
        top = self._task("top", milestone=m, status="Closed")
        mid = self._task("mid", milestone=m, parent=top)  # Open
        self._task("leaf", milestone=m, parent=mid, status="WIP")
        self._task("alone", milestone=m)
        self.assertEqual(self._counts(m), (4, 3))
        # without the flag: the rollup the agent tools share is unchanged
        self.assertEqual(milestone_work_counts([m])[m.milestone_id], (4, 1))

    def test_a_deleted_branch_is_not_counted(self):
        m = self._milestone("M")
        gone = self._task("gone", milestone=m, status="Deleted")
        child = self._task("child", milestone=m, parent=gone)
        self._task("grandchild", milestone=m, parent=child, status="Closed")
        self._task("alone", milestone=m)
        self.assertEqual(self._counts(m), (1, 0))

    def test_deleted_beats_closed_wherever_each_sits(self):
        m = self._milestone("M")
        deleted = self._task("deleted", milestone=m, status="Deleted")
        closed = self._task("closed", milestone=m, parent=deleted, status="Closed")
        self._task("open", milestone=m, parent=closed)
        self.assertEqual(self._counts(m), (0, 0))
        top = self._task("top", milestone=m, status="Closed")
        gone = self._task("gone", milestone=m, parent=top, status="Deleted")
        self._task("under", milestone=m, parent=gone)
        # `top` and nothing else: `gone` and `under` are Deleted by the rule
        self.assertEqual(self._counts(m), (1, 1))

    def test_a_closed_milestone_reads_n_of_n(self):
        m = self._milestone("M")
        self._task("a", milestone=m)
        self._task("b", milestone=m, status="WIP")
        self._task("c", parent=m.task, status="Blocked")  # attached by parent only
        self.assertEqual(self._counts(m), (3, 0))
        self._set(m.task, "Closed")
        self.assertEqual(self._counts(m), (3, 3))
        # reopening brings the children back exactly as they were
        self._set(m.task, "Open")
        self.assertEqual(self._counts(m), (3, 0))

    def test_a_closed_milestone_without_a_backing_row_yet(self):
        legacy = self._milestone("legacy", backing=False)
        self._task("a", milestone=legacy)
        self._task("b", milestone=legacy, status="Closed")
        MilestoneMaster.objects.filter(pk=legacy.pk).update(status="Closed")
        legacy.refresh_from_db()
        self.assertEqual(self._counts(legacy), (2, 2))

    def test_a_task_under_a_closed_task_of_another_milestone(self):
        a, b = self._milestone("A"), self._milestone("B")
        closed_in_b = self._task("closed-in-b", milestone=b, status="Closed")
        # A task of A that hangs under B's closed task: closed for A too.
        self._task("a-under-b", milestone=a, parent=closed_in_b)
        self.assertEqual(self._counts(a), (1, 1))

    def test_a_cycle_in_the_links_is_survived(self):
        m = self._milestone("M")
        one = self._task("one", milestone=m)
        two = self._task("two", milestone=m, parent=one)
        TaskMaster.objects.filter(pk=one.pk).update(parent_task_id=two.task_id)
        self.assertEqual(self._counts(m), (2, 0))
        self._set(one, "Closed")
        self.assertEqual(self._counts(m), (2, 2))

    def test_the_list_and_the_single_milestone_agree_and_cost_no_more_queries(self):
        m = self._milestone("M")
        top = self._task("top", milestone=m, status="Closed")
        self._task("child", milestone=m, parent=top)
        self._task("alone", milestone=m)

        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(
                reverse("project_milestones"), {"project_id": self.project.project_id}
            )
        self.assertEqual(res.status_code, 200, res.content)
        row = next(r for r in res.json()["milestones"] if r["milestoneId"] == m.milestone_id)
        self.assertEqual((row["tasksTotal"], row["tasksClosed"]), (3, 2))
        # the same budget as before the rule (test_milestone_list_queries.BUDGET)
        self.assertEqual(len(ctx), 7)

        single = _serialize_milestone(m)
        self.assertEqual((single["tasksTotal"], single["tasksClosed"]), (3, 2))

    def test_the_rule_agrees_with_a_plain_reading_of_it_on_random_trees(self):
        rng = random.Random(1389)
        milestones = [self._milestone(f"M{i}") for i in range(4)]
        tasks = []
        for i in range(120):
            parent = rng.choice([None, None, *tasks[-12:], *[m.task for m in milestones]])
            tasks.append(
                self._task(
                    f"t{i}",
                    milestone=rng.choice([None, *milestones]),
                    parent=parent,
                    status=rng.choice(["Open", "WIP", "Closed", "Closed", "Deleted", "Blocked"]),
                    is_deleted=rng.random() < 0.05,
                    is_init_task=rng.random() < 0.05,
                )
            )
        for m in rng.sample(milestones, 2):
            self._set(m.task, rng.choice(["Closed", "Deleted"]))

        rows = {
            t.task_id: t
            for t in TaskMaster.objects.filter(project=self.project, is_deleted=False).exclude(
                is_init_task=True
            )
        }
        backing = {t.milestone_id: t.task_id for t in rows.values() if t.is_milestone}

        def ancestors(t):
            seen, todo = set(), [t]
            while todo:
                cur = todo.pop()
                links = [cur.parent_task_id]
                if not cur.is_milestone and cur.milestone_id is not None:
                    links.append(backing.get(cur.milestone_id))
                for link in links:
                    if link in rows and link not in seen and link != t.task_id:
                        seen.add(link)
                        todo.append(rows[link])
            return [rows[i] for i in seen]

        def expected(m):
            total = closed = 0
            for t in rows.values():
                if t.task_id == m.task_id:
                    continue
                owned = t.milestone_id == m.milestone_id or (
                    t.parent_task_id == m.task_id and not t.is_milestone
                )
                if not owned or t.status == "Deleted":
                    continue
                above = {a.status for a in ancestors(t)}
                if "Deleted" in above:
                    continue
                total += 1
                closed += t.status == "Closed" or "Closed" in above
            return total, closed

        fresh = list(MilestoneMaster.objects.filter(project=self.project))
        got = milestone_work_counts(fresh, ancestor_rule=True)
        for m in fresh:
            with self.subTest(milestone=m.title):
                self.assertEqual(tuple(got[m.milestone_id]), expected(m))
        self.assertGreater(sum(v[0] for v in got.values()), 30)

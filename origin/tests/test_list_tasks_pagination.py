"""`list_tasks` pages (MIP-778).

It used to stop at 50 rows and say nothing: a milestone of 97 tasks came
back as 50, and a caller could not tell the list was partial, which 50 it
had, or how to reach the rest. Now `has_more` says so and `next_cursor`
continues, in a total order (`ts_updated_at` newest first, task id on ties).
"""

from datetime import timedelta

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class ListTasksPaginationTests(BaseAPITestCase):
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

    def _task(self, title, **kwargs):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title=title, **kwargs
        )

    def _make(self, n, **kwargs):
        return [self._task(f"t{i}", **kwargs) for i in range(n)]

    def _page(self, **args):
        return LIST_TASKS.run(args, self.ctx)

    def _walk(self, **args):
        """Every page of a query, as (page sizes, ids in order)."""
        pages, ids, cursor = [], [], None
        for _ in range(50):
            out = self._page(**args, **({"cursor": cursor} if cursor else {}))
            pages.append(len(out["tasks"]))
            ids += [t["task_id"] for t in out["tasks"]]
            if not out["has_more"]:
                self.assertNotIn("next_cursor", out)
                return pages, ids
            cursor = out["next_cursor"]
        self.fail("the cursor never ran out")

    def test_a_list_that_fits_is_not_partial(self):
        self._make(3)
        out = self._page(limit=5)
        self.assertEqual(len(out["tasks"]), 3)
        self.assertFalse(out["has_more"])
        self.assertNotIn("next_cursor", out)

    def test_exactly_a_full_page_is_not_partial(self):
        self._make(3)
        out = self._page(limit=3)
        self.assertEqual(len(out["tasks"]), 3)
        self.assertFalse(out["has_more"])

    def test_a_partial_list_says_so_and_pages_through_without_repeats_or_gaps(self):
        made = self._make(7)
        out = self._page(limit=3)
        self.assertTrue(out["has_more"])
        self.assertIn("more available", out["__summary__"])

        pages, ids = self._walk(limit=3)
        self.assertEqual(pages, [3, 3, 1])
        self.assertEqual(len(ids), 7)
        self.assertEqual(set(ids), {t.task_id for t in made})

    def test_order_is_newest_update_first(self):
        a, b, c = self._make(3)
        now = a.ts_updated_at
        for task, age in ((a, 3), (b, 1), (c, 2)):
            TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=now - timedelta(days=age))
        _, ids = self._walk(limit=2)
        self.assertEqual(ids, [b.task_id, c.task_id, a.task_id])

    def test_rows_with_the_same_update_time_are_ordered_by_id_and_never_split(self):
        made = self._make(6)
        stamp = made[0].ts_updated_at
        TaskMaster.objects.filter(pk__in=[t.pk for t in made]).update(ts_updated_at=stamp)
        pages, ids = self._walk(limit=2)
        self.assertEqual(pages, [2, 2, 2])
        self.assertEqual(ids, sorted((t.task_id for t in made), reverse=True))

    def test_a_milestone_larger_than_one_page_pages_in_full(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        backing = ensure_backing_task(milestone)
        direct = self._make(7, milestone=milestone)
        children = [self._task(f"child{i}", parent_task_id=backing.task_id) for i in range(4)]
        wanted = {t.task_id for t in direct + children}

        pages, ids = self._walk(milestone_id=milestone.milestone_id, limit=4)
        self.assertEqual(sum(pages), len(ids))
        self.assertEqual(len(ids), len(set(ids)), "a task came back twice")
        self.assertTrue(wanted <= set(ids), "a task was skipped")
        self.assertGreater(len(pages), 2)

    def test_filters_hold_across_pages(self):
        self._make(5, status="Open")
        wip = self._make(4, status="WIP")
        _, ids = self._walk(status=["WIP"], limit=3)
        self.assertEqual(set(ids), {t.task_id for t in wip})

    def test_a_deleted_task_takes_no_place_in_a_page(self):
        made = self._make(4)
        TaskMaster.objects.filter(pk=made[0].pk).update(status="Deleted")
        _, ids = self._walk(limit=3)
        self.assertEqual(set(ids), {t.task_id for t in made[1:]})

    def test_a_bad_cursor_is_refused_with_advice(self):
        for bad in ("nonsense", "e30", "!!!"):
            with self.assertRaisesRegex(ToolError, "next_cursor"):
                self._page(cursor=bad)

    def test_the_cursor_is_in_the_published_schema(self):
        self.assertIn("cursor", LIST_TASKS.parameters_schema["properties"])

"""MIP-1365: `GET /api/v2/task/progress/`, the web client's read of the progress facts.

What changed at or under a task or milestone, newest first. Each entry is the activity
row as the task's own feed serialises it, plus the task it happened to and its depth.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.signals.task_signals import update_tasks_recorded
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_task_progress_readers import Tree

URL = "/api/v2/task/progress/"


class TaskProgressEndpointTests(Tree, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Feed", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            self.build()
        # a clean slate: only what the test does is in the history
        self.age()

    def _get(self, task=None, expect=200, **params):
        res = self.client.get(URL, {"task_id": (task or self.ms).task_id, **params})
        self.assertEqual(res.status_code, expect, getattr(res, "data", None))
        return res.data

    # ---- what is returned --------------------------------------------------------

    def test_entries_carry_the_activity_the_task_that_changed_and_its_depth(self):
        self.edit(self.sub, status="Closed")
        data = self._get()
        entry = next(e for e in data["entries"] if e["actionType"] == "status_changed")
        self.assertEqual(entry["newValue"], "Closed")
        self.assertEqual(entry["depth"], 2)
        self.assertEqual(entry["sourceTask"]["id"], str(self.sub.task_id))
        self.assertEqual(entry["sourceTask"]["displayId"], self.sub.display_id)
        self.assertEqual(entry["sourceTask"]["title"], "Sub A-A")
        self.assertIn("activityId", entry)
        self.assertIn("tsCreatedAt", entry)
        self.assertIn("factId", entry)

    def test_it_is_the_same_row_the_tasks_own_feed_serialises(self):
        self.edit(self.sub, priority="High")
        mine = self.client.get(
            "/api/v2/task/activity/",
            {"team_id": str(self.team.team_id), "task_id": self.sub.task_id},
        ).data
        own = next(a for a in mine if a["actionType"] == "priority_changed")
        theirs = next(
            e for e in self._get(self.sub)["entries"] if e["actionType"] == "priority_changed"
        )
        for key, value in own.items():
            self.assertEqual(theirs[key], value, key)

    def test_a_task_only_lists_what_is_under_it_not_its_siblings(self):
        self.edit(self.sibling, priority="High")
        self.assertEqual(self._get(self.task)["entries"], [])

    def test_min_depth_leaves_out_its_own_changes(self):
        self.edit(self.task, priority="High")
        self.edit(self.sub, status="WIP")
        everything = {e["depth"] for e in self._get(self.task)["entries"]}
        under = {e["depth"] for e in self._get(self.task, min_depth=1)["entries"]}
        self.assertEqual(everything, {0, 1})
        self.assertEqual(under, {1})

    def test_newest_first(self):
        for status in ("WIP", "Pending", "Closed"):
            self.edit(self.sub, status=status)
        stamps = [e["tsCreatedAt"] for e in self._get()["entries"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_bulk_move_is_not_listed(self):
        with self.captureOnCommitCallbacks(execute=True):
            update_tasks_recorded(
                TaskMaster.objects.filter(pk=self.sub.pk), cascade="milestone_move", priority="High"
            )
        self.assertEqual(self._get()["entries"], [])

    # ---- paging -----------------------------------------------------------------

    def test_pages_cover_every_entry_once_and_the_last_has_no_cursor(self):
        for status in ("WIP", "Pending", "Closed", "Open", "WIP"):
            self.edit(self.sub, status=status)
        everything = [e["factId"] for e in self._get(limit=100)["entries"]]
        seen, cursor, pages = [], None, 0
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = self._get(**params)
            seen += [e["factId"] for e in page["entries"]]
            pages += 1
            cursor = page["nextCursor"]
            if cursor is None:
                break
            self.assertLess(pages, 20)
        self.assertEqual(seen, everything)
        self.assertEqual(len(set(seen)), len(seen))
        self.assertGreater(pages, 1)

    def test_entries_with_the_same_time_are_each_listed_once(self):
        """The cursor is (time, id), not time: facts written in one instant (a bulk
        import, a backfill) would otherwise be skipped or repeated at a page edge."""
        for status in ("WIP", "Pending", "Closed"):
            self.edit(self.sub, status=status)
        same = timezone.now()
        TaskProgressFact.objects.filter(ancestor_task=self.ms).update(ts=same)
        everything = [e["factId"] for e in self._get(limit=100)["entries"]]
        self.assertGreaterEqual(len(everything), 3)
        seen, cursor = [], None
        for _ in range(20):
            page = self._get(limit=1, **({"cursor": cursor} if cursor else {}))
            seen += [e["factId"] for e in page["entries"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
        self.assertEqual(seen, everything)

    def test_limit_is_capped(self):
        from unittest import mock

        from origin.views.task import task_progress_views

        for status in ("WIP", "Pending", "Closed"):
            self.edit(self.sub, status=status)
        with mock.patch.object(task_progress_views, "MAX_LIMIT", 2):
            self.assertEqual(len(self._get(limit=50)["entries"]), 2)

    def test_a_made_up_cursor_is_the_first_page_not_an_error(self):
        self.edit(self.sub, status="Closed")
        first = [e["factId"] for e in self._get()["entries"]]
        for junk in ("nonsense", "1_x", "99999999999999999999_1", "", "_"):
            self.assertEqual([e["factId"] for e in self._get(cursor=junk)["entries"]], first, junk)
        # A well-formed cursor from before everything is a real position, not junk: an
        # empty page, and still no error.
        self.assertEqual(self._get(cursor="-1_-1")["entries"], [])

    def test_limit_is_clamped_and_must_be_a_number(self):
        self.edit(self.sub, status="Closed")
        self.assertEqual(len(self._get(limit=0)["entries"]), 1)  # clamped up to 1
        self._get(expect=400, limit="many")
        self._get(expect=400, min_depth="x")

    def test_since(self):
        self.edit(self.sub, status="Closed")
        future = (timezone.now() + timedelta(days=1)).isoformat()
        past = (timezone.now() - timedelta(days=1)).isoformat()
        self.assertEqual(self._get(since=future)["entries"], [])
        self.assertTrue(self._get(since=past)["entries"])
        self._get(expect=400, since="last tuesday")

    # ---- access -----------------------------------------------------------------

    def test_a_non_member_assignee_is_refused_like_a_missing_task(self):
        outsider = self.user2
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=outsider)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=outsider).exists()
        )
        # sanity: the task's own feed lets the assignee in; this one must not
        self.client.force_authenticate(user=outsider)
        self._get(self.task, expect=404)

    def test_a_missing_deleted_or_unreadable_task_is_the_same_404(self):
        self._get(expect=200)
        res = self.client.get(URL, {"task_id": 99999999})
        self.assertEqual(res.status_code, 404)
        TaskMaster.objects.filter(pk=self.sub.pk).update(is_deleted=True)
        self._get(self.sub, expect=404)
        self.assertEqual(self.client.get(URL).status_code, 400)

    def test_another_teams_member_is_refused(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o2@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user2)
        self.client.force_authenticate(user=self.user2)
        self._get(expect=404)

    def test_it_needs_a_login(self):
        self.client.force_authenticate(user=None)
        self.client.credentials()
        self.assertIn(self.client.get(URL, {"task_id": self.ms.task_id}).status_code, (401, 403))

    # ---- cost -------------------------------------------------------------------

    def test_the_query_count_does_not_grow_with_the_number_of_entries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.edit(self.sub, status="WIP")
        with CaptureQueriesContext(connection) as small:
            self._get(limit=100)
        for status in ("Pending", "Closed", "Open", "WIP", "Pending", "Closed"):
            self.edit(self.sub, status=status)
            self.edit(self.sibling, priority="High" if status == "Closed" else "Low")
        self.assertGreater(TaskProgressFact.objects.filter(ancestor_task=self.ms).count(), 10)
        with CaptureQueriesContext(connection) as large:
            self.assertGreater(len(self._get(limit=100)["entries"]), 10)
        self.assertEqual(len(large), len(small), "queries grow with the number of entries")

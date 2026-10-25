"""The team's tasks can be fetched a page at a time (MIP-807).

`getTeamTasks` answers with a bare JSON array, which has nowhere to put a
cursor, so paging is a SEPARATE route, `getTeamTasksPage`. The old route is
untouched, and the things pinned here are the ones that go wrong at a page
boundary or when a second copy of a rule is written: a task skipped or
repeated, the access scope drifting from the unpaged route, and the two routes
describing a task differently.
"""

from django.contrib.auth import get_user_model

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

OLD = "/api/v2/task/getTeamTasks/"
PAGE = "/api/v2/task/getTeamTasksPage/"


class TeamTasksPagingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        sysuser = User.objects.create_user(
            username="ttpsys", email="ttpsys@example.com", password="pw"
        )
        sysuser.is_system_user = True
        sysuser.save(update_fields=["is_system_user"])
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, project_system_user=sysuser
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Theirs", owner=self.user2, project_system_user=sysuser
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user2
        )
        self.authenticate(self.user)

    def _task(self, title, project=None, **extra):
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            title=title,
            status="Open",
            reporter=self.user,
            **extra,
        )

    def _make(self, n):
        return [self._task(f"t{i:03d}") for i in range(n)]

    def _get(self, url=PAGE, **params):
        res = self.client.get(url, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res.data

    def _all_pages(self, limit):
        rows, cursor, pages = [], None, 0
        for _ in range(60):
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}))
            pages += 1
            rows += body["tasks"]
            if not body["has_more"]:
                return rows, pages
            cursor = body["next_cursor"]
        self.fail("paging did not end")

    # ── the paged route ───────────────────────────────────────────────

    def test_pages_cover_every_task_exactly_once(self):
        made = self._make(7)

        rows, pages = self._all_pages(limit=3)

        self.assertEqual(pages, 3)
        self.assertEqual([r["id"] for r in rows], [str(t.task_id) for t in made])

    def test_a_page_that_fits_has_no_cursor(self):
        self._make(3)

        body = self._get(limit=3)

        self.assertEqual(len(body["tasks"]), 3)
        self.assertIs(body["has_more"], False)
        self.assertIsNone(body["next_cursor"])

    def test_an_exact_multiple_of_the_page_size_does_not_invent_a_page(self):
        self._make(6)

        rows, pages = self._all_pages(limit=3)

        self.assertEqual((len(rows), pages), (6, 2))

    def test_an_empty_team_is_one_empty_page(self):
        body = self._get()

        self.assertEqual(body, {"tasks": [], "has_more": False, "next_cursor": None})

    def test_a_task_created_between_pages_is_not_repeated_or_skipped_before_the_cursor(self):
        made = self._make(4)
        first = self._get(limit=2)
        newer = self._task("late")

        second = self._get(limit=10, cursor=first["next_cursor"])

        self.assertEqual(
            [r["id"] for r in second["tasks"]],
            [str(made[2].task_id), str(made[3].task_id), str(newer.task_id)],
        )

    def test_an_unreadable_cursor_is_the_first_page(self):
        made = self._make(3)

        for bad in ("not-base64!!", "e30=", "bnVsbA=="):
            with self.subTest(cursor=bad):
                body = self._get(limit=2, cursor=bad)
                self.assertEqual(body["tasks"][0]["id"], str(made[0].task_id))

    def test_limit_is_clamped_and_junk_falls_back(self):
        self._make(3)

        self.assertEqual(len(self._get(limit=0)["tasks"]), 1)
        self.assertEqual(len(self._get(limit="abc")["tasks"]), 3)
        self.assertEqual(len(self._get(limit=10**9)["tasks"]), 3)

    def test_team_id_is_required(self):
        res = self.client.get(PAGE)

        self.assertEqual(res.status_code, 400)

    def test_the_page_query_is_ordered_by_the_primary_key(self):
        """Without an ORDER BY a page is whatever the database returns first,
        and a boundary can skip or repeat rows once the table is big enough to
        be read out of insertion order. That cannot be forced on a small test
        table, so this checks the query itself (as the inbox paging test does)."""
        from unittest import mock

        from django.db.models.query import QuerySet

        seen = []
        real = QuerySet.__getitem__

        def spy(qs, key):
            if getattr(qs, "model", None) is TaskMaster and isinstance(key, slice):
                seen.append(tuple(qs.query.order_by))
            return real(qs, key)

        self._make(3)
        with mock.patch.object(QuerySet, "__getitem__", spy):
            self._get(limit=2)

        self.assertIn(("task_id",), seen)

    # ── access: one rule, not two ─────────────────────────────────────

    def test_it_hides_a_project_i_am_not_in(self):
        self._task("Visible")
        self._task("Hidden", project=self.other_project)

        rows, _ = self._all_pages(limit=1)

        self.assertEqual({r["title"] for r in rows}, {"Visible"})

    def test_the_hidden_init_task_is_not_listed(self):
        """Each project owns a bookkeeping task that is not a user's task; the
        unpaged route leaves it out, so this one must too."""
        self._task("real")
        self._task("bookkeeping", is_init_task=True)

        rows, _ = self._all_pages(limit=1)

        self.assertEqual([r["title"] for r in rows], ["real"])

    def test_a_non_member_of_the_team_is_refused(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw"
        )
        self.authenticate(outsider)

        res = self.client.get(PAGE, {"team_id": str(self.team.team_id)})

        self.assertIn(res.status_code, (403, 404))

    def test_it_returns_the_same_tasks_as_the_unpaged_route_for_every_caller(self):
        """The two routes share one scope, so they may not disagree about who
        sees what. Compared as sets of ids for each of two different callers."""
        self._task("a")
        self._task("b", project=self.other_project)
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )
        self._task("orphan", project=None)
        for caller in (self.user, self.user2):
            with self.subTest(caller=caller.username):
                self.authenticate(caller)
                old = {r["id"] for r in self._get(OLD)}
                new = {r["id"] for r in self._all_pages(limit=1)[0]}
                self.assertEqual(new, old)

    def test_a_row_is_byte_identical_to_the_unpaged_routes(self):
        """Both build the row with `_team_task_row`; a copy that drifted would
        show here."""
        self._make(2)

        old = self._get(OLD)
        new, _ = self._all_pages(limit=1)

        self.assertEqual(new, old)

    # ── the unpaged route is unchanged ────────────────────────────────

    def test_the_unpaged_route_is_still_a_bare_array_and_ignores_paging_params(self):
        self._make(3)

        body = self._get(OLD, limit=1, cursor="whatever")

        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 3)

"""MIP-1990: `GET /api/v2/search/tasks/`, the task bar's typed search.

Any task in a project the caller can reach (Closed included, Deleted never), found by title or by
display id, newest first, paged, with the project, status and assignee on every row.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster

from .test_base import BaseAPITestCase

User = get_user_model()
URL = "/api/v2/search/tasks/"


class TaskQuerySearchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project("Website", "WEB")
        self.authenticate()

    def _project(self, name, code, team=None, owner=None, member=True):
        team = team or self.team
        owner = owner or self.user
        project = ProjectMaster.objects.create(
            team=team, project_name=name, code=code, owner=owner, project_system_user=owner
        )
        if member:
            ProjectMembers.objects.create(team=team, project=project, attendee=owner)
        return project

    def _task(self, title, project=None, **kwargs):
        project = project or self.project
        kwargs.setdefault("status", "Open")
        task = TaskMaster.objects.create(
            team=project.team, project=project, reporter=self.user, title=title, **kwargs
        )
        task.refresh_from_db()
        return task

    def _get(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get(URL, params)

    def _ids(self, res):
        return [r["taskId"] for r in res.data["results"]]

    def test_finds_by_title_case_insensitive_with_the_row_fields(self):
        task = self._task("Fix the Login bug", assignee=self.user)
        self._task("Unrelated")
        res = self._get(q="login")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._ids(res), [task.task_id])
        row = res.data["results"][0]
        self.assertEqual(row["projectName"], "Website")
        self.assertEqual(row["displayId"], task.display_id)
        self.assertEqual(row["status"]["status"], "Open")
        self.assertEqual(row["assigneeName"], self.user.username)
        self.assertFalse(res.data["hasMore"])

    def test_closed_tasks_are_found_and_deleted_ones_never(self):
        closed = self._task("Old thing", status="Closed")
        deleted = self._task("Old gone thing", status="Deleted")
        flagged = self._task("Old flagged thing", is_deleted=True)
        ids = self._ids(self._get(q="old"))
        self.assertIn(closed.task_id, ids)
        self.assertNotIn(deleted.task_id, ids)
        self.assertNotIn(flagged.task_id, ids)

    def test_finds_by_display_id_every_spelling(self):
        task = self._task("Alpha")
        for spelled in (task.display_id, task.display_id.lower(), f"#{task.display_id}"):
            self.assertIn(task.task_id, self._ids(self._get(q=spelled)), spelled)
        self.assertIn(task.task_id, self._ids(self._get(q=str(task.project_task_number))))

    def test_an_id_match_comes_before_a_title_match(self):
        title_hit = self._task("see WEB-1 for details")
        id_hit = self._task("Alpha")
        res = self._get(q=title_hit.display_id)
        self.assertEqual(self._ids(res)[0], title_hit.task_id)
        res = self._get(q="WEB-%d" % id_hit.project_task_number)
        self.assertEqual(self._ids(res)[0], id_hit.task_id)

    def test_percent_and_underscore_are_literal(self):
        a = self._task("100% done")
        self._task("1000 done")
        self.assertEqual(self._ids(self._get(q="100%")), [a.task_id])

    def test_is_paged_newest_first(self):
        tasks = [self._task(f"page {i}") for i in range(5)]
        first = self._get(q="page", limit=2)
        self.assertEqual(len(first.data["results"]), 2)
        self.assertTrue(first.data["hasMore"])
        second = self._get(q="page", limit=2, offset=2)
        last = self._get(q="page", limit=2, offset=4)
        got = self._ids(first) + self._ids(second) + self._ids(last)
        self.assertEqual(got, [t.task_id for t in reversed(tasks)])
        self.assertFalse(last.data["hasMore"])

    def test_limit_is_capped_and_bad_numbers_are_a_400(self):
        self.assertEqual(self._get(q="x", limit=100000).status_code, 200)
        for bad in ({"limit": "abc"}, {"offset": "-1"}, {"limit": "0"}):
            self.assertEqual(self._get(q="x", **bad).status_code, 400, bad)

    def test_project_and_status_filters_make_the_starting_list(self):
        other = self._project("Mobile", "MOB")
        a = self._task("a open")
        self._task("a closed", status="Closed")
        self._task("b open", project=other)
        res = self._get(project_id=self.project.project_id, statuses="open,wip", limit=5)
        self.assertEqual(self._ids(res), [a.task_id])

    def test_a_project_the_caller_is_not_in_is_a_404(self):
        owner = User.objects.create_user(username="own", email="own@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=owner)
        other = self._project("Mobile", "MOB", owner=owner)
        self.assertEqual(self._get(project_id=other.project_id).status_code, 404)

    def test_other_teams_and_non_member_projects_never_appear(self):
        mine = self._task("shared word mine")
        # same team, project the caller is not a member of
        owner = User.objects.create_user(username="own", email="own@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=owner)
        hidden_same_team = self._project("Hidden", "HID", owner=owner)
        hidden_task = self._task("shared word hidden", project=hidden_same_team)
        # a different team altogether
        outsider = User.objects.create_user(username="out", email="out@example.com", password="x")
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        secret_project = self._project("Secret", "SEC", team=other_team, owner=outsider)
        secret = self._task("shared word secret", project=secret_project)
        ids = self._ids(self._get(q="shared word"))
        self.assertEqual(ids, [mine.task_id])
        self.assertNotIn(hidden_task.task_id, ids)
        self.assertNotIn(secret.task_id, ids)
        # asking in the other team's name does not widen anything either
        res = self._get(q="shared word", team_id=str(other_team.team_id))
        self.assertNotIn(secret.task_id, self._ids(res))
        self.assertNotIn(mine.task_id, self._ids(res))

    def test_a_missing_or_bad_team_is_a_400(self):
        self.assertEqual(self.client.get(URL, {"q": "x"}).status_code, 400)
        self.assertEqual(self.client.get(URL, {"q": "x", "team_id": "nope"}).status_code, 400)

    def test_requires_login(self):
        self.client.credentials()
        self.assertIn(self._get(q="x").status_code, (401, 403))

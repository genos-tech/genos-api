"""Public API v1 — the surface strangers write code against.

Three properties matter more than the endpoints themselves, so they get
the most tests:

  * **A key sees exactly what its person sees.** Every read goes through
    `scope_guards`, the same helpers the app uses, so the public API
    cannot become a second, laxer ACL that drifts from the first.
  * **Scope is enforced on the METHOD.** A read-only key cannot reach a
    write path — including one added after this file was written.
  * **A session cannot drive it.** The credential an integrator manages
    is the credential the limits and audit attach to.
"""

from django.contrib.auth import get_user_model

from origin.models.common.api_key_models import (
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

ME = "/api/public/v1/me/"
PROJECTS = "/api/public/v1/projects/"
TASKS = "/api/public/v1/tasks/"


class PublicApiBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Public Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Visible", status="Open", reporter=self.user
        )

        # A project in the same team that `self.user` is NOT in — the
        # control for "a key sees what its person sees", not "what the
        # team has".
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Not Mine", owner=self.user2
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user2
        )
        self.other_task = TaskMaster.objects.create(
            team=self.team,
            project=self.other_project,
            title="Hidden",
            status="Open",
            reporter=self.user2,
        )

    def _key(self, user=None, scope=SCOPE_WRITE, team=None):
        raw = generate_key()
        ApiKey.objects.create(
            user=user or self.user,
            team=team,
            name="k",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=scope,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        return raw

    def _q(self, extra=""):
        return f"?team_id={self.team.team_id}{extra}"


class TestCredentialRules(PublicApiBase):
    def test_a_key_can_call_the_api(self):
        self._key()
        res = self.client.get(ME)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(str(res.data["user_id"]), str(self.user.id))

    def test_a_session_jwt_is_refused(self):
        """The public API is for automation; a browser session driving it
        would key limits and audit on the wrong thing."""
        self.authenticate(self.user)
        self.assertEqual(self.client.get(ME).status_code, 403)

    def test_unauthenticated_is_refused(self):
        self.unauthenticate()
        self.assertIn(self.client.get(ME).status_code, (401, 403))

    def test_a_revoked_key_stops_working(self):
        raw = self._key()
        ApiKey.objects.get(key_hash=hash_key(raw)).revoke()
        self.assertEqual(self.client.get(ME).status_code, 401)


class TestScopeEnforcement(PublicApiBase):
    def test_a_read_key_may_read(self):
        self._key(scope=SCOPE_READ)
        self.assertEqual(self.client.get(TASKS + self._q()).status_code, 200)

    def test_a_read_key_may_not_write(self):
        self._key(scope=SCOPE_READ)
        res = self.client.post(
            TASKS,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "title": "nope",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(TaskMaster.objects.filter(title="nope").exists())

    def test_a_read_key_may_not_patch(self):
        self._key(scope=SCOPE_READ)
        res = self.client.patch(f"{TASKS}{self.task.task_id}/", {"title": "nope"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_a_write_key_may_write(self):
        self._key(scope=SCOPE_WRITE)
        res = self.client.post(
            TASKS,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "title": "made by a bot",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["reporter_id"], str(self.user.id))


class TestScoping(PublicApiBase):
    def test_projects_are_the_persons_projects_not_the_teams(self):
        self._key()
        res = self.client.get(PROJECTS + self._q())
        names = {p["name"] for p in res.data["projects"]}
        self.assertEqual(names, {"Public Project"})

    def test_tasks_default_to_the_persons_projects(self):
        self._key()
        res = self.client.get(TASKS + self._q())
        titles = {t["title"] for t in res.data["tasks"]}
        self.assertEqual(titles, {"Visible"})

    def test_naming_a_project_you_are_not_in_is_a_404(self):
        self._key()
        res = self.client.get(TASKS + self._q(f"&project_id={self.other_project.project_id}"))
        self.assertEqual(res.status_code, 404)

    def test_a_task_you_cannot_see_is_a_404(self):
        self._key()
        res = self.client.get(f"{TASKS}{self.other_task.task_id}/")
        self.assertEqual(res.status_code, 404)

    def test_creating_in_a_project_you_are_not_in_is_a_404(self):
        self._key()
        res = self.client.post(
            TASKS,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.other_project.project_id,
                "title": "planted",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskMaster.objects.filter(title="planted").exists())

    def test_a_foreign_team_is_refused(self):
        outsider = User.objects.create_user(
            username="pubout", email="pubout@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Public Other", team_email="pubother@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        self._key(user=outsider)
        res = self.client.get(PROJECTS + self._q())
        self.assertEqual(res.status_code, 400)


class TestTeamBinding(PublicApiBase):
    def test_a_personal_token_must_name_a_team(self):
        self._key()
        self.assertEqual(self.client.get(PROJECTS).status_code, 400)

    def test_a_team_key_needs_no_team_id(self):
        self._key(team=self.team)
        res = self.client.get(PROJECTS)
        self.assertEqual(res.status_code, 200)

    def test_a_team_key_ignores_a_different_team_id(self):
        """Otherwise the binding would be advisory."""
        other_owner = User.objects.create_user(
            username="bindother", email="bindother@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Bind Other", team_email="bindother@example.com", owner=other_owner
        )
        self._key(team=self.team)
        res = self.client.get(f"{PROJECTS}?team_id={other_team.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual({p["name"] for p in res.data["projects"]}, {"Public Project"})

    # ── a team key lasts only as long as the membership (MIP-718) ─────

    def _leave(self, user, team):
        """A bare row flip, NOT `remove_team_member`: that also revokes the
        key now, and these tests are about the per-request re-check that
        has to hold without it (a key minted before the cascade existed,
        or a membership ended by some path that skips it)."""
        TeamMembers.objects.filter(team=team, attendee=user).update(is_deleted=True)

    def test_a_team_key_stops_working_once_its_owner_leaves(self):
        """`resolve_team` returned a team key's team unconditionally, so
        a departed member kept full REST access for the key's lifetime.
        MCP re-checked; REST never did."""
        self._key(user=self.user2, team=self.team)
        self.assertEqual(self.client.get(PROJECTS).status_code, 200)
        self._leave(self.user2, self.team)
        self.assertEqual(self.client.get(PROJECTS).status_code, 400)
        self.assertEqual(self.client.get(TASKS).status_code, 400)
        res = self.client.post(
            TASKS,
            {"project_id": self.other_project.project_id, "title": "after leaving"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(TaskMaster.objects.filter(title="after leaving").exists())

    def test_a_departed_members_team_key_cannot_open_a_task_by_id(self):
        """`/tasks/<id>/` never resolves a team, so the membership check
        has to reach it separately — and `can_access_task` alone still
        says yes, because a project row outlives a bare membership flip."""
        self._key(user=self.user2, team=self.team)
        url = f"{TASKS}{self.other_task.task_id}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self._leave(self.user2, self.team)
        self.assertEqual(self.client.get(url).status_code, 404)
        res = self.client.patch(url, {"title": "rewritten"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.other_task.refresh_from_db()
        self.assertEqual(self.other_task.title, "Hidden")

    def _second_team(self):
        """A second team `self.user` belongs to, with a project and task
        of theirs in it — what a team key bound to `self.team` must not
        reach, though the person behind it can."""
        other_team = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        project = ProjectMaster.objects.create(
            team=other_team, project_name="Second Project", owner=self.user
        )
        ProjectMembers.objects.create(team=other_team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=other_team, project=project, title="Elsewhere", status="Open", reporter=self.user
        )
        return other_team, project, task

    def test_a_team_key_cannot_open_its_owners_task_in_another_team(self):
        _, _, task = self._second_team()
        self._key(team=self.team)
        url = f"{TASKS}{task.task_id}/"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"title": "x"}, format="json").status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.title, "Elsewhere")

    def test_a_personal_token_still_spans_its_owners_teams(self):
        """The control for the test above: the narrowing is the binding's,
        not a new limit on the person."""
        _, _, task = self._second_team()
        self._key()
        self.assertEqual(self.client.get(f"{TASKS}{task.task_id}/").status_code, 200)

    def test_a_team_key_cannot_file_a_task_into_another_teams_project(self):
        """Project membership was the only check, so a key bound to team A
        wrote a `team=A` task into the owner's team-B project."""
        _, project, _ = self._second_team()
        self._key(team=self.team)
        res = self.client.post(
            TASKS, {"project_id": project.project_id, "title": "crossed"}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskMaster.objects.filter(title="crossed").exists())

    def test_a_personal_token_cannot_cross_teams_on_create_either(self):
        _, project, _ = self._second_team()
        self._key()
        res = self.client.post(
            TASKS,
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
                "title": "crossed",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskMaster.objects.filter(title="crossed").exists())


class TestWriteSurface(PublicApiBase):
    def test_patch_uses_an_allowlist(self):
        """A public API that forwarded arbitrary keys would let every new
        model field silently join the contract."""
        self._key()
        res = self.client.patch(
            f"{TASKS}{self.task.task_id}/",
            {
                "title": "renamed",
                "team": "00000000-0000-0000-0000-000000000000",
                "is_deleted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "renamed")
        self.assertFalse(self.task.is_deleted)
        self.assertEqual(str(self.task.team_id), str(self.team.team_id))

    def test_patch_with_no_writable_field_is_a_400(self):
        self._key()
        res = self.client.patch(f"{TASKS}{self.task.task_id}/", {"is_deleted": True}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_an_empty_title_is_refused(self):
        self._key()
        res = self.client.patch(f"{TASKS}{self.task.task_id}/", {"title": "   "}, format="json")
        self.assertEqual(res.status_code, 400)


class TestPagination(PublicApiBase):
    def test_limit_is_capped(self):
        self._key()
        res = self.client.get(TASKS + self._q("&limit=5000"))
        self.assertEqual(res.data["limit"], 100)

    def test_bad_paging_is_a_400_not_a_500(self):
        self._key()
        self.assertEqual(self.client.get(TASKS + self._q("&limit=abc")).status_code, 400)

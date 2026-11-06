"""A project create takes only the fields a client may set (MIP-1018).

`ProjectMasterView.post` copied the body into a `fields = "__all__"`
serializer, so any model column the body named was written: `is_deleted:
true` answered 201 and made a deleted project, and the image path, the
per-project task rules and the template defaults were set around the
endpoints that validate them.

The rule, shared with `owner` (MIP-1016): a server-owned field carrying a
value other than the server's own is refused with a 400 and nothing is
written; the server's own value (`is_deleted: false`) is accepted and never
used. `project_system_user` is server-owned too (MIP-1031): the server makes
the project's system user, so a body that names one is refused.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PROJECT = "/api/v2/project/"


class ProjectCreateAllowListTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _counts(self):
        return (ProjectMaster.objects.count(), ProjectMembers.objects.count())

    def _post(self, name, **extra):
        body = {"team": str(self.team.team_id), "project_name": name, "owner": str(self.user.id)}
        body.update(extra)
        return self.client.post(PROJECT, body, format="json")

    def _system_user(self, name="sys"):
        return User.objects.create_user(
            username=name, email=f"{name}@genos.tech", password=None, is_system_user=True
        )

    # ── server-owned fields ───────────────────────────────────────────

    def test_a_forged_server_field_is_refused_and_writes_nothing(self):
        forged = {
            "is_deleted": True,
            "profile_image_file_name": "project_profiles/9/elsewhere.png",
            "profile_image_url": "project_profiles/9/elsewhere.png",
            "default_task_template": "custom:1",
            "default_milestone_template": "milestone",
            "task_field_rules": {"dueDate": {"required": True}},
            "project_system_user": str(self.user2.id),
        }
        before = self._counts()
        for i, (field, value) in enumerate(forged.items()):
            with self.subTest(field=field):
                res = self._post(f"Forged{i}", **{field: value})
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": f"{field} cannot be set."})
        self.assertEqual(self._counts(), before)

    def test_is_deleted_true_no_longer_makes_a_deleted_project(self):
        """The reported case: it answered 201 and stored `is_deleted=True`."""
        res = self._post("Ghost", is_deleted=True)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ProjectMaster.objects.filter(project_name="Ghost").exists())

    def test_the_servers_own_value_is_accepted_and_unused(self):
        echoes = (
            {"is_deleted": False},
            {"is_deleted": "false"},
            {"is_deleted": None},
            {"profile_image_file_name": None},
            {"profile_image_file_name": ""},
            {"default_task_template": None, "default_milestone_template": None},
            {"task_field_rules": {}},
        )
        for i, extra in enumerate(echoes):
            with self.subTest(body=extra):
                res = self._post(f"Echo{i}", **extra)
                self.assertEqual(res.status_code, 201, res.data)
                project = ProjectMaster.objects.get(project_id=res.data["project_id"])
                self.assertFalse(project.is_deleted)
                self.assertEqual(project.task_field_rules, {})
                self.assertIsNone(project.default_task_template)
                self.assertIsNone(project.profile_image_file_name)

    def test_an_unknown_or_read_only_key_is_ignored(self):
        res = self._post("Extras", project_id=9999, ts_created_at="2001-01-01T00:00:00Z", junk=1)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["project_id"], 9999)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        self.assertGreater(project.ts_created_at.year, 2001)

    # ── what the app sends ────────────────────────────────────────────

    def test_the_create_modals_exact_body_still_works(self):
        """`ModalCreateProject`: team, project_name, owner, is_private."""
        res = self.client.post(
            PROJECT,
            {
                "team": str(self.team.team_id),
                "project_name": "From the modal",
                "owner": str(self.user.id),
                "is_private": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        system_user = project.project_system_user
        self.assertTrue(system_user.is_system_user)
        self.assertTrue(project.is_private)
        self.assertEqual(project.owner_id, self.user.id)
        self.assertFalse(project.is_deleted)
        self.assertEqual(project.team_id, self.team.team_id)
        self.assertTrue(project.code)
        # The keys the modal reads back.
        self.assertEqual(res.data["project_name"], "From the modal")
        self.assertEqual(str(res.data["project_system_user"]), str(system_user.id))

    def test_the_first_run_bodys_exact_shape_still_works(self):
        """`TaskInit`: team, project_name, owner. No system user, not private.

        The server makes the project's system user (MIP-1031)."""
        res = self.client.post(
            PROJECT,
            {
                "team": str(self.team.team_id),
                "project_name": "First",
                "owner": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        self.assertFalse(project.is_private)
        self.assertTrue(project.project_system_user.is_system_user)

    # ── project_system_user ───────────────────────────────────────────

    def test_a_named_project_system_user_is_refused(self):
        """Not a colleague's, not the caller's, not an unclaimed system user."""
        unclaimed = self._system_user("unclaimed")
        bad = (
            str(unclaimed.id),
            str(self.user2.id),
            str(self.user.id),
            "00000000-0000-0000-0000-000000000000",
            "not-a-uuid",
            5,
            [str(self.user2.id)],
        )
        before = self._counts()
        users = User.objects.count()
        for i, value in enumerate(bad):
            with self.subTest(project_system_user=value):
                res = self._post(f"Sys{i}", project_system_user=value)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": "project_system_user cannot be set."})
        self.assertEqual(self._counts(), before)
        self.assertEqual(User.objects.count(), users)

    def test_an_empty_project_system_user_means_none(self):
        for i, value in enumerate((None, "")):
            with self.subTest(project_system_user=value):
                res = self._post(f"NoSys{i}", project_system_user=value)
                self.assertEqual(res.status_code, 201, res.data)

    # ── code ──────────────────────────────────────────────────────────

    def test_a_supplied_code_is_checked_like_the_edit_checks_it(self):
        before = self._counts()
        for i, code in enumerate(("a-b", "X", "TOOLONGCODE", "1AB", "AB CD")):
            with self.subTest(code=code):
                res = self._post(f"Code{i}", code=code)
                self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(self._counts(), before)

    def test_a_valid_code_is_kept_in_upper_case(self):
        res = self._post("Coded", code=" gen2 ")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["code"], "GEN2")

    def test_an_absent_code_is_derived(self):
        res = self._post("Derived Name")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertRegex(res.data["code"], r"^[A-Z][A-Z0-9]{1,5}$")

    # ── is_private is a client field ──────────────────────────────────

    def test_is_private_is_the_clients_to_set(self):
        res = self._post("Hidden", is_private=True)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(ProjectMaster.objects.get(project_id=res.data["project_id"]).is_private)

    # ── the team ──────────────────────────────────────────────────────

    def test_a_project_cannot_be_planted_in_a_team_the_caller_is_not_in(self):
        outsider = User.objects.create_user(
            username="pcaout", email="pcaout@example.com", password="pw"
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="oth@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other, attendee=outsider)
        before = self._counts()
        res = self.client.post(
            PROJECT,
            {"team": str(other.team_id), "project_name": "Planted", "owner": str(self.user.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self._counts(), before)

    def test_a_second_team_id_in_the_body_does_not_move_the_project(self):
        outsider = User.objects.create_user(
            username="pcaout2", email="pcaout2@example.com", password="pw"
        )
        other = TeamMaster.objects.create(
            team_name="Other2", team_email="oth2@example.com", owner=outsider
        )
        res = self._post("Stays", team_id=str(other.team_id))
        self.assertEqual(res.status_code, 201, res.data)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        self.assertEqual(project.team_id, self.team.team_id)


class ProjectUpdateIgnoresSystemFieldsTests(BaseAPITestCase):
    """`PUT /project/` reads `code`, `project_name` and `owner_id` by name.

    It never passed the body to a serializer, so a forged system field was
    already dropped. Pinned here so a later rewrite onto the serializer
    cannot quietly open it.
    """

    def test_a_forged_system_field_changes_nothing(self):
        self.authenticate(self.user)
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Edit me",
            owner=self.user,
            project_system_user=self.user,
            code="EDIT",
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        res = self.client.put(
            PROJECT,
            {
                "project_id": project.project_id,
                "is_deleted": True,
                "is_private": True,
                "project_system_user": str(self.user2.id),
                "profile_image_file_name": "x/y.png",
                "task_field_rules": {"junk": 1},
                "team": str(self.team.team_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        project.refresh_from_db()
        self.assertFalse(project.is_deleted)
        self.assertFalse(project.is_private)
        self.assertEqual(project.project_system_user_id, self.user.id)
        self.assertIsNone(project.profile_image_file_name)
        self.assertEqual(project.task_field_rules, {})

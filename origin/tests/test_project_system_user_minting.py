"""A project's system user is made by the server (MIP-1031).

The create modal used to sign a system user up through the open signup
endpoint and then name it on the project, so anyone could mint system users
(and reserve any email address with one). The project create now makes the
account itself, in the same transaction as the project, and refuses a body
that names one.

A project with no system user (rows from before this, or one whose account
was removed: the FK is SET_NULL) must still read: the task endpoints used to
answer 500 on `t.project.project_system_user.id`.
"""

from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PROJECT = "/api/v2/project/"


class ProjectCreateMintsItsSystemUserTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _post(self, name, **extra):
        body = {"team": str(self.team.team_id), "project_name": name, "owner": str(self.user.id)}
        body.update(extra)
        return self.client.post(PROJECT, body, format="json")

    def _system_users(self):
        return User.objects.filter(is_system_user=True).count()

    def test_a_create_without_one_makes_the_system_user_in_the_team(self):
        before = self._system_users()
        res = self._post("Fresh Start")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self._system_users(), before + 1)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        sys_user = project.project_system_user
        self.assertTrue(sys_user.is_system_user)
        self.assertFalse(sys_user.has_usable_password())
        self.assertEqual(sys_user.username, "Fresh Start")
        # The address is the server's, not a client's.
        self.assertEqual(sys_user.email, f"{self.team.team_id}-Fresh_Start@genos.tech")
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=sys_user).exists())
        self.assertEqual(str(res.data["project_system_user"]), str(sys_user.id))
        self.assertEqual(len(mail.outbox), 0)

    def test_the_modals_new_body_has_no_system_user_and_works(self):
        res = self._post("New modal", is_private=True)
        self.assertEqual(res.status_code, 201, res.data)
        project = ProjectMaster.objects.get(project_id=res.data["project_id"])
        self.assertTrue(project.is_private)
        self.assertTrue(project.project_system_user_id)
        # The modal still joins the owner to the project afterwards.
        join = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
                "attendee_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(join.status_code, 201, join.data)
        self.assertTrue(ProjectMembers.objects.filter(project=project, attendee=self.user).exists())

    def test_a_refused_create_leaves_no_system_user(self):
        self.assertEqual(self._post("Same Name").status_code, 201)
        users, projects = User.objects.count(), ProjectMaster.objects.count()
        res = self._post("Same Name")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(ProjectMaster.objects.count(), projects)

    def test_a_stranger_cannot_make_a_system_user_through_a_create(self):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw-stranger-1"
        )
        users = User.objects.count()
        self.authenticate(stranger)
        res = self._post("Planted")  # a team the caller is not on
        self.assertIn(res.status_code, (403, 404), res.data)
        self.assertEqual(User.objects.count(), users)
        anon = APIClient()
        res = anon.post(
            PROJECT,
            {"team": str(self.team.team_id), "project_name": "Anon", "owner": str(self.user.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(User.objects.count(), users)

    def test_a_repeated_name_gets_a_distinct_address(self):
        """A deleted project's name can come back; the second account must not collide."""
        first = self._post("Again")
        ProjectMaster.objects.filter(project_id=first.data["project_id"]).update(
            project_name="Again (old)"
        )
        second = self._post("Again")
        self.assertEqual(second.status_code, 201, second.data)
        emails = set(User.objects.filter(is_system_user=True).values_list("email", flat=True))
        self.assertEqual(len(emails), 2)

    def test_a_page_that_names_a_system_user_is_refused(self):
        """The old modal's body: nothing is made, and nothing is claimed."""
        old = User.objects.create_user(
            username="old", email="old@genos.tech", password=None, is_system_user=True
        )
        users, projects = User.objects.count(), ProjectMaster.objects.count()
        res = self._post("Old page", project_system_user=str(old.id))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(ProjectMaster.objects.count(), projects)


class ProjectWithoutSystemUserReadsTests(BaseAPITestCase):
    """The FK is nullable and SET_NULL, so a project can have none."""

    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="No account", code="NA", owner=self.user
        )
        self.assertIsNone(self.project.project_system_user_id)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="t",
            status="Open",
            reporter=self.user,
            chat_type=1,
            chat_id="chat-1",
            thread_id="thread-1",
        )

    def test_get_task_answers_with_a_null_system_user(self):
        res = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertIsNone(res.data[0]["project"]["systemUserId"])

    def test_get_task_by_thread_answers_with_a_null_system_user(self):
        res = self.client.get(
            "/api/v2/task/getTaskByThreadId/",
            {
                "team_id": str(self.team.team_id),
                "chat_type": 1,
                "chat_id": "chat-1",
                "thread_id": "thread-1",
            },
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertIsNone(res.data[0]["project"]["systemUserId"])

    def test_the_same_reads_still_carry_a_system_user_when_there_is_one(self):
        sys_user = User.objects.create_user(
            username="sys", email="sys@genos.tech", password=None, is_system_user=True
        )
        ProjectMaster.objects.filter(pk=self.project.pk).update(project_system_user=sys_user)
        res = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(str(res.data[0]["project"]["systemUserId"]), str(sys_user.id))

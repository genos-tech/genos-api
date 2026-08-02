"""Task reads that trusted a client-supplied scope.

Seven `high` findings from ACL_AUDIT.md, in two shapes:

  **Team-wide lists** (`getTeamTasks`, `getTeamTasksByTag`) filtered on
  nothing but a `team_id` the caller supplied, returning every task in
  that team — with assignee emails attached. They now scope to the
  caller's PROJECTS, which is what `TaskMetaView` already did and what
  the public API does.

  **Single objects** (`getTask`, `getTaskByThreadId`, comments,
  dependencies, child tasks) resolved from a walkable integer id with no
  membership check.

`bystander_task` is the control throughout: a task in the same team, in
a project `self.user` does not belong to. It must be invisible on every
one of these surfaces.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class TaskReadScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # Real projects always carry a system user (the create flow signs
        # one up); the task serializer dereferences it, so the fixture
        # has to as well.
        sysuser = User.objects.create_user(
            username="trssys", email="trssys@example.com", password="pw"
        )
        sysuser.is_system_user = True
        sysuser.save(update_fields=["is_system_user"])

        # Mine.
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mine",
            owner=self.user,
            project_system_user=sysuser,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Visible",
            status="Open",
            reporter=self.user,
        )

        # Same team, a project self.user is NOT in.
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            owner=self.user2,
            project_system_user=sysuser,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user2
        )
        self.bystander_task = TaskMaster.objects.create(
            team=self.team,
            project=self.other_project,
            title="Hidden",
            status="Open",
            reporter=self.user2,
            chat_type=3,
            chat_id="chat-abc",
            thread_id="thread-abc",
        )
        TaskComments.objects.create(
            task=self.bystander_task,
            comment_id=1,
            sender=self.user2,
            comment_body={"text": "secret discussion"},
        )

    def _auth(self):
        self.authenticate(self.user)

    # ── team-wide lists ───────────────────────────────────────────────

    def test_team_task_list_is_scoped_to_my_projects(self):
        self._auth()
        res = self.client.get("/api/v2/task/getTeamTasks/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        titles = {t["title"] for t in res.data}
        self.assertIn("Visible", titles)
        self.assertNotIn("Hidden", titles)

    def test_team_task_by_tag_is_scoped_to_my_projects(self):
        self._auth()
        res = self.client.get(
            "/api/v2/task/getTeamTasksByTag/", {"team_id": str(self.team.team_id)}
        )
        self.assertEqual(res.status_code, 200)
        project_ids = {p["projectId"] for p in res.data}
        self.assertNotIn(self.other_project.project_id, project_ids)

    def test_a_member_of_both_still_sees_both(self):
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )
        self._auth()
        res = self.client.get("/api/v2/task/getTeamTasks/", {"team_id": str(self.team.team_id)})
        self.assertEqual({t["title"] for t in res.data}, {"Visible", "Hidden"})

    # ── single objects ────────────────────────────────────────────────

    def test_get_task_refuses_a_task_i_cannot_see(self):
        self._auth()
        res = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.other_project.project_id,
                "task_id": self.bystander_task.task_id,
            },
        )
        self.assertEqual(res.status_code, 404)

    def test_get_task_still_works_for_mine(self):
        self._auth()
        res = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "task_id": self.task.task_id,
            },
        )
        self.assertEqual(res.status_code, 200)

    def test_comments_refuse_a_task_i_cannot_see(self):
        """Comments carry their authors' identities."""
        self._auth()
        res = self.client.get("/api/v2/task/comment/", {"task_id": self.bystander_task.task_id})
        self.assertEqual(res.status_code, 404)

    def test_dependencies_refuse_a_task_i_cannot_see(self):
        self._auth()
        res = self.client.get(
            "/api/v2/task/dependency/list/", {"task_id": self.bystander_task.task_id}
        )
        self.assertEqual(res.status_code, 404)

    def test_child_tasks_refuse_a_project_i_am_not_in(self):
        self._auth()
        res = self.client.get(
            "/api/v2/task/childTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.other_project.project_id,
                "current_task_id": self.bystander_task.task_id,
            },
        )
        self.assertEqual(res.status_code, 404)

    def test_thread_lookup_returns_empty_rather_than_someone_elses_task(self):
        """Guarded on the RESULT — the lookup is by thread, so there is
        no task id to check up front. Empty body rather than 404: the
        caller may legitimately be in the chat while the linked task
        lives in a project they are not in."""
        self._auth()
        res = self.client.get(
            "/api/v2/task/getTaskByThreadId/",
            {
                "team_id": str(self.team.team_id),
                "chat_type": 3,
                "chat_id": "chat-abc",
                "thread_id": "thread-abc",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {})

    def test_thread_lookup_still_resolves_my_own_task(self):
        self.task.chat_type = 3
        self.task.chat_id = "chat-mine"
        self.task.thread_id = "thread-mine"
        self.task.save(update_fields=["chat_type", "chat_id", "thread_id"])
        self._auth()
        res = self.client.get(
            "/api/v2/task/getTaskByThreadId/",
            {
                "team_id": str(self.team.team_id),
                "chat_type": 3,
                "chat_id": "chat-mine",
                "thread_id": "thread-mine",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertNotEqual(res.data, {})

    # ── assignee parity ───────────────────────────────────────────────

    def test_an_assignee_without_a_project_row_still_sees_their_task(self):
        """`can_access_task` mirrors the search ACL, so a task someone
        can find must also be one they can open."""
        assignee = User.objects.create_user(
            username="trsassignee", email="trsassignee@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=assignee)
        self.bystander_task.assignee = assignee
        self.bystander_task.save(update_fields=["assignee"])

        self.authenticate(assignee)
        res = self.client.get(
            "/api/v2/task/getTask/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.other_project.project_id,
                "task_id": self.bystander_task.task_id,
            },
        )
        self.assertEqual(res.status_code, 200)

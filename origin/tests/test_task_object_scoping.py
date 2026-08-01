"""Task objects addressed by a walkable integer id, with no membership check.

`TaskMaster.task_id` is a `BigAutoField`, so every task in the install is
reachable by counting. Four endpoints resolved a task from that id alone:

  * `TaskMasterView.put`    — rewrite any task
  * `TaskMasterView.delete` — **hard** delete (`task.delete()`), i.e.
                              unrecoverable loss, not a soft delete
  * `TaskActivityListView`  — the task's full edit history with actors
  * `GetSearchTeamTasksView` — search any project by naming it

The last one is the most instructive: its `project_id == -1` branch
derives scope from `ProjectMembers` correctly, which made the endpoint
look guarded. Passing a project id explicitly skipped that branch — so
the default path was safe and the parameterised one was not.

Access follows `scope_guards.can_access_task`: project members plus the
assignee and reporter, matching `agent/acl.task_acl_user_ids` so search
and the API cannot disagree about who may open a task.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class TaskObjectScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Scoped Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Private task",
            status="Open",
            reporter=self.user,
        )

        # A separate tenant — the attacker.
        self.outsider = User.objects.create_user(
            username="taskout", email="taskout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="Task Outsider", team_email="taskout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── the hard delete ───────────────────────────────────────────────

    def test_outsider_cannot_hard_delete_a_task(self):
        """Unrecoverable: the handler calls task.delete(), not a soft delete."""
        self.authenticate(self.outsider)
        res = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}"
            f"&task_id={self.task.task_id}&is_init_task_boolean=0"
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(TaskMaster.objects.filter(task_id=self.task.task_id).exists())

    def test_member_can_still_delete_their_own_task(self):
        self.authenticate(self.user)
        res = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}"
            f"&task_id={self.task.task_id}&is_init_task_boolean=0"
        )
        self.assertEqual(res.status_code, 204)
        self.assertFalse(TaskMaster.objects.filter(task_id=self.task.task_id).exists())

    # ── edit ──────────────────────────────────────────────────────────

    def test_outsider_cannot_edit_a_task(self):
        self.authenticate(self.outsider)
        res = self.client.put(
            "/api/v2/task/",
            {"task_id": self.task.task_id, "title": "pwned"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Private task")

    def test_member_can_still_edit(self):
        self.authenticate(self.user)
        res = self.client.put(
            "/api/v2/task/",
            {"task_id": self.task.task_id, "title": "renamed"},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "renamed")

    def test_assignee_without_a_project_row_may_edit(self):
        """can_access_task mirrors task_acl_user_ids, which grants the
        assignee access without a ProjectMembers row — search already
        shows them the task."""
        assignee = User.objects.create_user(
            username="taskassignee", email="taskassignee@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=assignee)
        self.task.assignee = assignee
        self.task.save(update_fields=["assignee"])
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=assignee).exists()
        )

        self.authenticate(assignee)
        res = self.client.put(
            "/api/v2/task/",
            {"task_id": self.task.task_id, "title": "assignee edit"},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))

    # ── audit log ─────────────────────────────────────────────────────

    def test_outsider_cannot_read_the_activity_log(self):
        self.authenticate(self.outsider)
        res = self.client.get(
            "/api/v2/task/activity/",
            {"team_id": str(self.team.team_id), "task_id": self.task.task_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_member_can_read_the_activity_log(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/task/activity/",
            {"team_id": str(self.team.team_id), "task_id": self.task.task_id},
        )
        self.assertEqual(res.status_code, 200)

    # ── team-task search ──────────────────────────────────────────────

    def _search(self, project_id):
        return self.client.get(
            "/api/v2/search/teamTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project_id,
                "statuses": "open",
                "top_n": 10,
            },
        )

    def test_explicit_project_id_is_membership_checked(self):
        """The -1 branch was correctly scoped; naming a project skipped it."""
        self.authenticate(self.outsider)
        res = self._search(self.project.project_id)
        self.assertEqual(res.status_code, 404)

    def test_explicit_project_id_works_for_a_member(self):
        self.authenticate(self.user)
        res = self._search(self.project.project_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([t["taskId"] for t in res.data], [self.task.task_id])

    def test_all_projects_branch_still_scopes_to_membership(self):
        self.authenticate(self.outsider)
        res = self._search(-1)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])

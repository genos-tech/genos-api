"""`create_task` assigns at creation (MIP-706).

The assignee is accepted on exactly `assign_task`'s terms — an active
team member, or someone on the target project — and omitting it leaves
the task unassigned: there is no implicit "assign to the caller".
"""

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK

from .test_base import BaseAPITestCase


class CreateTaskAssigneeTests(BaseAPITestCase):
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

    def _create(self, **args):
        out = CREATE_TASK.run(
            {"title": "T", "project_id": self.project.project_id, **args}, self.ctx
        )
        return TaskMaster.objects.get(task_id=out["task_id"]), out

    def _outsider(self, email="outsider@example.com"):
        return CustomUser.objects.create_user(email=email, username="outsider", password="x")

    def _task_count(self):
        return TaskMaster.objects.filter(is_milestone=False).count()

    def test_assign_yourself_at_creation(self):
        task, out = self._create(assignee_id=str(self.user.id))
        self.assertEqual(str(task.assignee_id), str(self.user.id))
        self.assertEqual(out["assignee_id"], str(self.user.id))
        self.assertEqual(out["assignee_username"], self.user.username)
        self.assertIn(f"assigned to {self.user.username}", out["__summary__"])

    def test_assign_a_teammate_who_is_not_on_the_project(self):
        # user2 is a TeamMembers row only — the team half of the guard.
        task, _ = self._create(assignee_id=str(self.user2.id))
        self.assertEqual(str(task.assignee_id), str(self.user2.id))

    def test_assign_a_project_guest_who_is_not_on_the_team(self):
        # A shared-project collaborator holds ProjectMembers, no TeamMembers.
        guest = self._outsider("guest@example.com")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        task, _ = self._create(assignee_id=str(guest.id))
        self.assertEqual(str(task.assignee_id), str(guest.id))

    def test_omitted_or_null_assignee_leaves_task_unassigned(self):
        for args in ({}, {"assignee_id": None}, {"assignee_id": ""}, {"assignee_id": "null"}):
            task, out = self._create(**args)
            self.assertIsNone(task.assignee_id, args)
            self.assertIsNone(out["assignee_id"], args)
            self.assertNotIn("assigned to", out["__summary__"])

    def test_user_outside_team_and_project_is_refused(self):
        outsider = self._outsider()
        with self.assertRaisesRegex(ToolError, "not an active member"):
            self._create(assignee_id=str(outsider.id))
        self.assertEqual(self._task_count(), 0)

    def test_member_of_another_team_is_refused(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        foreign = self._outsider("foreign@example.com")
        TeamMembers.objects.create(team=other_team, attendee=foreign)
        with self.assertRaisesRegex(ToolError, "not an active member"):
            self._create(assignee_id=str(foreign.id))
        self.assertEqual(self._task_count(), 0)

    def test_removed_team_member_is_refused(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        with self.assertRaisesRegex(ToolError, "not an active member"):
            self._create(assignee_id=str(self.user2.id))
        self.assertEqual(self._task_count(), 0)

    def test_deleted_account_is_refused(self):
        CustomUser.objects.filter(id=self.user2.id).update(is_deleted=True)
        with self.assertRaisesRegex(ToolError, "has been deleted"):
            self._create(assignee_id=str(self.user2.id))
        self.assertEqual(self._task_count(), 0)

    def test_unknown_or_garbage_id_is_refused(self):
        for raw in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
            with self.assertRaises(ToolError, msg=raw):
                self._create(assignee_id=raw)
        self.assertEqual(self._task_count(), 0)

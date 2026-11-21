"""The in-app agent's approved writes are refused in an archived project (MIP-1917).

`create_task`, `update_task`, `add_comment`, `assign_task`, `update_task_dependencies` and
`manage_project_members` write straight to the database, so the services' archive guard cannot reach
them. The controller checks when the approved tool RUNS (approval can come after the archive), with the
arguments the tool will receive, and hands the model the shared sentence as the tool's error.
"""

from __future__ import annotations

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.archive import ARCHIVED_MESSAGE
from origin.tests.test_agent_decide_edits import DecideEditBase


class ArchivedAgentWriteTests(DecideEditBase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Old",
            code="OLD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            project=self.project, team=self.team, title="Existing", reporter=self.user
        )
        self.other_task = TaskMaster.objects.create(
            project=self.project, team=self.team, title="Other", reporter=self.user
        )

    def archive(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(archived_at=timezone.now())

    def _refused(self, events):
        (error,) = self.of_type(events, "tool_call_error")
        self.assertEqual(error["error"], ARCHIVED_MESSAGE)
        self.assertEqual(self.of_type(events, "tool_call_result"), [])

    # real tools: nothing is written
    def test_create_task_is_refused(self):
        self.archive()
        run, token, _ = self.pause(
            "create_task", {"title": "New", "project_id": self.project.project_id}
        )
        self._refused(self.events(self.post(run, token)))
        self.assertFalse(TaskMaster.objects.filter(title="New").exists())

    def test_add_comment_is_refused(self):
        self.archive()
        run, token, _ = self.pause(
            "add_comment", {"task_id": self.task.task_id, "body_text": "hello"}
        )
        self._refused(self.events(self.post(run, token)))
        self.assertFalse(TaskComments.objects.filter(task=self.task).exists())

    def test_the_check_runs_at_approval_not_at_proposal(self):
        run, token, _ = self.pause(
            "add_comment", {"task_id": self.task.task_id, "body_text": "hello"}
        )
        self.archive()  # archived between the proposal and the click
        self._refused(self.events(self.post(run, token)))
        self.assertFalse(TaskComments.objects.filter(task=self.task).exists())

    # the other tools: the stand-in records whether it ran
    def _assert_refused_without_running(self, tool_name, arguments):
        tool = self.fake(tool_name)[tool_name]
        self.archive()
        run, token, _ = self.pause(tool_name, arguments)
        self._refused(self.events(self.post(run, token)))
        self.assertEqual(tool.calls, [])

    def test_update_task_is_refused(self):
        self._assert_refused_without_running(
            "update_task", {"task_id": self.task.task_id, "title": "Renamed"}
        )

    def test_assign_task_is_refused(self):
        self._assert_refused_without_running(
            "assign_task", {"task_id": self.task.task_id, "assignee_id": str(self.user.id)}
        )

    def test_update_task_dependencies_is_refused(self):
        self._assert_refused_without_running(
            "update_task_dependencies",
            {
                "add": [
                    {
                        "blocker_task_id": self.task.task_id,
                        "blocked_task_id": self.other_task.task_id,
                    }
                ]
            },
        )

    def test_adding_a_project_member_is_refused(self):
        self._assert_refused_without_running(
            "manage_project_members",
            {
                "action": "add",
                "project_id": self.project.project_id,
                "user_id": str(self.other.id),
            },
        )

    # control: the same writes run while the project is not archived
    def test_the_same_write_runs_in_an_active_project(self):
        tool = self.fake("assign_task")["assign_task"]
        run, token, _ = self.pause(
            "assign_task", {"task_id": self.task.task_id, "assignee_id": str(self.user.id)}
        )
        self.assertEqual(self.of_type(self.events(self.post(run, token)), "tool_call_error"), [])
        self.assertEqual(len(tool.calls), 1)

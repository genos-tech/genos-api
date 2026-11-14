"""MIP-1516: `create_note` for a task note does not say which project ids exist.

`create_note(note_type="task", project_id=N)` answered "Project N not found." for an id that is
not there but "Not authorized: project belongs to a different team." for one that is, in another
team: a project-existence oracle for the model (and for a person's AI app over MCP), one id at a
time. The two are now the same words. Only the CLASS differs, as it does for the note tools
(MIP-1512): a project that exists in another team is a `ToolForbidden`, which keeps counting toward
the MCP refusal alarm, and a missing one is a `ToolError`; what the model reads never differs.

Nobody gains anything: the same callers create task notes, and the same ones are refused.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
MISSING = 99999999


class CreateTaskNoteProjectAnswers(BaseAPITestCase):
    """`self.user` is on `self.team` and a member of its project; the stranger's project lives in
    another team that `self.user` does not belong to."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        other_owner = User.objects.create_user(
            username="other-owner", email="oo@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=other_owner
        )
        TeamMembers.objects.create(team=self.other_team, attendee=other_owner)
        self.foreign = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Theirs",
            owner=other_owner,
            project_system_user=other_owner,
        )
        self.deleted = ProjectMaster.objects.create(
            team=self.team,
            project_name="Gone",
            owner=self.user,
            project_system_user=self.user,
            is_deleted=True,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def create(self, project_id, ctx=None):
        return create_note_run(
            {"note_type": "task", "title": "n", "project_id": project_id}, ctx or self.ctx
        )

    def refusal(self, project_id, ctx=None):
        with self.assertRaises(ToolError) as raised:
            self.create(project_id, ctx)
        return raised.exception

    def test_a_project_in_another_team_reads_like_a_missing_one(self):
        missing = self.refusal(MISSING)
        foreign = self.refusal(self.foreign.project_id)
        self.assertEqual(str(missing), f"Project {MISSING} not found.")
        self.assertEqual(
            str(foreign).replace(str(self.foreign.project_id), "N"),
            str(missing).replace(str(MISSING), "N"),
        )
        self.assertEqual(str(foreign), f"Project {self.foreign.project_id} not found.")

    def test_a_deleted_project_reads_the_same_too(self):
        self.assertEqual(
            str(self.refusal(self.deleted.project_id)),
            f"Project {self.deleted.project_id} not found.",
        )

    def test_the_class_still_tells_the_refusal_alarm_which_it_was(self):
        self.assertNotIsInstance(self.refusal(MISSING), ToolForbidden)
        self.assertIsInstance(self.refusal(self.foreign.project_id), ToolForbidden)

    def test_nothing_is_created_in_the_other_team(self):
        self.refusal(self.foreign.project_id)
        self.assertFalse(TaskNoteMaster.objects.filter(project=self.foreign).exists())

    def test_a_member_still_creates_the_note(self):
        res = self.create(self.project.project_id)
        self.assertTrue(
            TaskNoteMaster.objects.filter(project=self.project, owner=self.user).exists(), res
        )

    def test_a_teammate_outside_the_project_keeps_the_answer_for_that(self):
        # unchanged on purpose: they are in the team, so the project is not a secret from them
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolForbidden) as raised:
            self.create(self.project.project_id, ctx)
        self.assertEqual(
            str(raised.exception),
            f"Not authorized to create notes in project {self.project.project_id}.",
        )

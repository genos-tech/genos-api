"""A hidden project reads as a missing one to a non-member (MIP-1947).

Before, the project tools answered "not a member" for a real hidden project and "not found" for a
missing id, so a teammate could confirm that a hidden project id exists. Each tool now gives the
SAME text for both, and still raises ToolForbidden so the MCP refusal alarm counts it.
"""

from __future__ import annotations

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.list_project_members import LIST_PROJECT_MEMBERS
from origin.search_engine.agent.tools.list_project_tags import LIST_PROJECT_TAGS
from origin.search_engine.agent.tools.manage_project import MANAGE_PROJECT
from origin.search_engine.agent.tools.manage_project_members import MANAGE_PROJECT_MEMBERS
from origin.search_engine.agent.tools.manage_sprint import MANAGE_SPRINT
from origin.tests.test_base import BaseAPITestCase

MISSING = 987654


def _message(tool, args, ctx) -> tuple[str, type]:
    try:
        tool.run(args, ctx)
    except (ToolError, ToolForbidden) as exc:
        return str(exc), type(exc)
    raise AssertionError("expected a refusal")


class HiddenProjectNotFoundTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.hidden = self._project("Codename Falcon", "FAL", hidden=True, private=True)
        self.private = self._project("Quiet", "QUI", private=True)
        self.outsider = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.member = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.hidden, title="Launch"
        )

    def _project(self, name, code, hidden=False, private=False):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            is_private=private,
            is_hidden=hidden,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _same_as_missing(self, tool, build_args):
        hidden_text, hidden_cls = _message(tool, build_args(self.hidden.project_id), self.outsider)
        missing_text, _ = _message(tool, build_args(MISSING), self.outsider)
        self.assertEqual(
            hidden_text.replace(str(self.hidden.project_id), "N"),
            missing_text.replace(str(MISSING), "N"),
            tool.name,
        )
        # The refusal alarm still counts it.
        self.assertTrue(issubclass(hidden_cls, ToolForbidden), tool.name)

    def test_project_tools_answer_like_a_missing_project(self):
        cases = [
            (GET_PROJECT_SUMMARY, lambda p: {"project_id": p}),
            (LIST_PROJECT_MEMBERS, lambda p: {"project_id": p}),
            (LIST_PROJECT_TAGS, lambda p: {"project_id": p}),
            (MANAGE_PROJECT, lambda p: {"action": "update", "project_id": p, "name": "x"}),
            (
                MANAGE_PROJECT_MEMBERS,
                lambda p: {"action": "add", "project_id": p, "user_id": str(self.user2.id)},
            ),
            (CREATE_TASK, lambda p: {"project_id": p, "title": "t"}),
            (
                CREATE_NOTE,
                lambda p: {"note_type": "task", "project_id": p, "title": "n", "content_text": "x"},
            ),
            (
                MANAGE_SPRINT,
                lambda p: {
                    "action": "create",
                    "project_id": p,
                    "name": "S",
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-14",
                },
            ),
        ]
        for tool, build in cases:
            with self.subTest(tool=tool.name):
                self._same_as_missing(tool, build)

    def test_milestone_of_hidden_project_reads_as_missing(self):
        hidden_text, cls = _message(
            GET_MILESTONE_SUMMARY, {"milestone_id": self.milestone.milestone_id}, self.outsider
        )
        missing_text, _ = _message(GET_MILESTONE_SUMMARY, {"milestone_id": MISSING}, self.outsider)
        self.assertEqual(
            hidden_text.replace(str(self.milestone.milestone_id), "N"),
            missing_text.replace(str(MISSING), "N"),
        )
        self.assertTrue(issubclass(cls, ToolForbidden))

    def test_member_still_reaches_a_hidden_project(self):
        res = GET_PROJECT_SUMMARY.run({"project_id": self.hidden.project_id}, self.member)
        self.assertIn("project", str(res).lower())

    def test_private_project_keeps_its_membership_answer(self):
        text, _ = _message(
            GET_PROJECT_SUMMARY, {"project_id": self.private.project_id}, self.outsider
        )
        self.assertIn("not a member", text)

    def test_leave_project_hidden_vs_missing_vs_private(self):
        def leave(project_id):
            return self.client.post(
                "/api/v2/project/leave/",
                {
                    "team_id": str(self.team.team_id),
                    "project_id": project_id,
                    "attendee_id": str(self.user2.id),
                },
                format="json",
            )

        self.client.force_authenticate(user=self.user2)
        hidden, missing, private = (
            leave(self.hidden.project_id),
            leave(MISSING),
            leave(self.private.project_id),
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.json(), missing.json())
        self.assertIn("not a member", private.json()["error"])

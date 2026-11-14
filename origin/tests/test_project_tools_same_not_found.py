"""MIP-1519: the project tools do not say which project ids exist in other teams.

`create_task`, `get_project_summary`, `list_project_members`, `list_project_tags`, the shared
`project_access` / `sprint_access` loaders and `task_reparent` answered "Project N not found." for
an id that is not there but "Not authorized: project belongs to a different team." (or "...is in a
different team.") for one that is, in another team: a project-existence oracle for the model and
for a person's AI app over MCP, one id at a time. The same split sat on the milestone and the
sprint of `create_task` / `sprint_access`, and on a parent task in `find_parent`. Each pair is now
the same words, the pattern of `milestone_access.load_project` and of `create_note` (MIP-1516).

Only the CLASS differs, so the MCP refusal alarm still counts the other-team case: a thing that
exists in another team is a `ToolForbidden`, a missing one a `ToolError`. What the model reads
never differs. Nobody gains anything: the same people are refused. A teammate who is in the team
but not in the project keeps their own, different answer (the project is no secret from them).
"""

from __future__ import annotations

import re

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import project_access, sprint_access
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_task import _run as create_task_run
from origin.search_engine.agent.tools.get_project_summary import _run as project_summary_run
from origin.search_engine.agent.tools.list_project_members import _run as project_members_run
from origin.search_engine.agent.tools.list_project_tags import _run as project_tags_run
from origin.services.task_reparent import find_parent, project_move_error
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
MISSING = 99999999


def shape(text: str) -> str:
    """The message with its ids blanked, so a missing id and a foreign one compare equal."""
    return re.sub(r"\d+", "N", str(text))


class Base(BaseAPITestCase):
    """`self.user` is on `self.team` and in its project; the foreign rows live in another team
    that `self.user` does not belong to."""

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
        self.foreign_milestone = MilestoneMaster.objects.create(
            team=self.other_team, project=self.foreign, title="M", reporter=other_owner
        )
        self.foreign_sprint = Sprint.objects.create(
            team=self.other_team,
            project=self.foreign,
            name="S",
            sequence_number=1,
            start_date="2026-08-01",
            end_date="2026-08-14",
        )
        self.foreign_task = TaskMaster.objects.create(
            team=self.other_team,
            project=self.foreign,
            title="T",
            reporter=other_owner,
            assignee=other_owner,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def refusal(self, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except ToolError as e:  # ToolForbidden is a ToolError
            return e
        self.fail("expected a refusal")

    def assert_same_words(self, missing, foreign, expected_foreign_text):
        self.assertEqual(shape(foreign), shape(missing))
        self.assertEqual(str(foreign), expected_foreign_text)


class ProjectToolsAnswerAlike(Base):
    def project_tool_cases(self):
        yield (
            "create_task",
            lambda pid: create_task_run({"title": "t", "project_id": pid}, self.ctx),
        )
        yield "get_project_summary", lambda pid: project_summary_run({"project_id": pid}, self.ctx)
        yield "list_project_members", lambda pid: project_members_run({"project_id": pid}, self.ctx)
        yield "list_project_tags", lambda pid: project_tags_run({"project_id": pid}, self.ctx)

    def test_a_project_in_another_team_reads_like_a_missing_one(self):
        for name, call in self.project_tool_cases():
            with self.subTest(tool=name):
                missing = self.refusal(call, MISSING)
                foreign = self.refusal(call, self.foreign.project_id)
                self.assertEqual(str(missing), f"Project {MISSING} not found.")
                self.assert_same_words(
                    missing, foreign, f"Project {self.foreign.project_id} not found."
                )

    def test_the_class_still_tells_the_refusal_alarm_which_it_was(self):
        for name, call in self.project_tool_cases():
            with self.subTest(tool=name):
                self.assertNotIsInstance(self.refusal(call, MISSING), ToolForbidden)
                self.assertIsInstance(self.refusal(call, self.foreign.project_id), ToolForbidden)

    def test_a_teammate_outside_the_project_keeps_the_answer_for_that(self):
        # unchanged on purpose: they are in the team, so the project is not a secret from them
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        for run in (project_summary_run, project_members_run, project_tags_run):
            with self.subTest(tool=run.__module__):
                with self.assertRaises(ToolForbidden) as raised:
                    run({"project_id": self.project.project_id}, ctx)
                self.assertIn("not a member of that project", str(raised.exception))

    def test_a_member_still_reads_the_project(self):
        out = project_members_run({"project_id": self.project.project_id}, self.ctx)
        self.assertTrue(out)

    def test_create_task_says_the_same_of_a_milestone_in_another_team(self):
        def call(mid):
            return create_task_run(
                {"title": "t", "project_id": self.project.project_id, "milestone_id": mid},
                self.ctx,
            )

        missing = self.refusal(call, MISSING)
        foreign = self.refusal(call, self.foreign_milestone.milestone_id)
        self.assertEqual(str(missing), f"Milestone {MISSING} not found.")
        self.assert_same_words(
            missing, foreign, f"Milestone {self.foreign_milestone.milestone_id} not found."
        )
        self.assertNotIsInstance(missing, ToolForbidden)
        self.assertIsInstance(foreign, ToolForbidden)
        self.assertFalse(TaskMaster.objects.filter(title="t").exists())


class SharedLoadersAnswerAlike(Base):
    def test_project_access_loader(self):
        missing = self.refusal(project_access.load_project, MISSING, self.ctx)
        foreign = self.refusal(project_access.load_project, self.foreign.project_id, self.ctx)
        self.assertEqual(
            str(missing), f"Project {MISSING} not found. Get ids from `list_projects`."
        )
        self.assert_same_words(
            missing,
            foreign,
            f"Project {self.foreign.project_id} not found. Get ids from `list_projects`.",
        )
        self.assertNotIsInstance(missing, ToolForbidden)
        self.assertIsInstance(foreign, ToolForbidden)

    def test_sprint_access_project_and_sprint_loaders(self):
        missing = self.refusal(sprint_access.load_project, MISSING, self.ctx)
        foreign = self.refusal(sprint_access.load_project, self.foreign.project_id, self.ctx)
        self.assert_same_words(missing, foreign, f"Project {self.foreign.project_id} not found.")
        self.assertNotIsInstance(missing, ToolForbidden)
        self.assertIsInstance(foreign, ToolForbidden)

        missing = self.refusal(sprint_access.load_sprint, MISSING, self.ctx)
        foreign = self.refusal(sprint_access.load_sprint, self.foreign_sprint.sprint_id, self.ctx)
        self.assertEqual(str(missing), f"Sprint {MISSING} not found. Get ids from `list_sprints`.")
        self.assert_same_words(
            missing,
            foreign,
            f"Sprint {self.foreign_sprint.sprint_id} not found. Get ids from `list_sprints`.",
        )
        self.assertNotIsInstance(missing, ToolForbidden)
        self.assertIsInstance(foreign, ToolForbidden)


class TaskReparentAnswersAlike(Base):
    def test_a_parent_task_in_another_team_reads_like_a_missing_one(self):
        _, missing = find_parent(MISSING, self.team.team_id)
        parent, foreign = find_parent(self.foreign_task.task_id, self.team.team_id)
        self.assertIsNone(parent)
        self.assertEqual(missing, f"Task {MISSING} not found.")
        self.assert_same_words(missing, foreign, f"Task {self.foreign_task.task_id} not found.")

    def test_a_real_parent_still_resolves(self):
        mine = TaskMaster.objects.create(
            team=self.team, project=self.project, title="P", reporter=self.user, assignee=self.user
        )
        parent, error = find_parent(mine.task_id, self.team.team_id)
        self.assertIsNone(error)
        self.assertEqual(parent.task_id, mine.task_id)

    def test_a_project_in_another_team_reads_like_a_missing_one_even_for_its_member(self):
        # the only caller who gets past the membership test of a foreign project
        ProjectMembers.objects.create(
            team=self.other_team, project=self.foreign, attendee=self.user
        )
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="M", reporter=self.user, assignee=self.user
        )
        missing = project_move_error(task, MISSING, self.team.team_id, self.user.id)
        foreign = project_move_error(task, self.foreign.project_id, self.team.team_id, self.user.id)
        self.assertEqual(missing, f"Project {MISSING} not found.")
        self.assert_same_words(missing, foreign, f"Project {self.foreign.project_id} not found.")

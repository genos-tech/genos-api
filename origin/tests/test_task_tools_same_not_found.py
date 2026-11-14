"""MIP-1531: every task / milestone tool answers "missing" and "in another team" with ONE message.

MIP-1519 (api #796) did this for the project, milestone, sprint and parent-task tools of the
project family. The task tools still said "Task N not found." for an id that is not there but
"Not authorized: task is in a different team." for one that is: an existence oracle over every
team's task ids, one id at a time, for the model and for a person's AI app over MCP. The same
split sat on a milestone in `get_milestone_summary` / `list_tasks` / `list_task_dependencies` /
`task_milestone_move`, on the project, parent task and milestone of `create_task_plan`, and on a
DELETED task of another team ("has been deleted." came before the team check, so it told a deleted
task from a missing one) and a deleted sprint of another team.

Only the CLASS still differs (a thing in another team is a `ToolForbidden`, a missing one a
`ToolError`), so the MCP refusal alarm keeps counting the other-team case; what the model reads
never differs. Nobody gains access: the same people are refused. A teammate who is in the team but
not on the project keeps their own, different answer: the task is no secret from them.
"""

from __future__ import annotations

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import sprint_access, task_access
from origin.search_engine.agent.tools.add_comment import _run as add_comment_run
from origin.search_engine.agent.tools.add_task_delivery import _run as add_delivery_run
from origin.search_engine.agent.tools.assign_task import _run as assign_task_run
from origin.search_engine.agent.tools.base import ToolContext, ToolForbidden
from origin.search_engine.agent.tools.create_task_plan import _run as create_task_plan_run
from origin.search_engine.agent.tools.fetch_task import _run as fetch_task_run
from origin.search_engine.agent.tools.get_milestone_summary import _run as milestone_summary_run
from origin.search_engine.agent.tools.get_task_blockers import _run as task_blockers_run
from origin.search_engine.agent.tools.get_task_history import _run as task_history_run
from origin.search_engine.agent.tools.list_task_dependencies import _run as dependencies_run
from origin.search_engine.agent.tools.list_tasks import _run as list_tasks_run
from origin.search_engine.agent.tools.remove_task_delivery import _run as remove_delivery_run
from origin.search_engine.agent.tools.update_task import _run as update_task_run
from origin.search_engine.agent.tools.update_tasks_bulk import _run as update_bulk_run
from origin.services.task_milestone_move import milestone_move_error
from origin.tests.test_project_tools_same_not_found import MISSING, shape
from origin.tests.test_project_tools_same_not_found import Base as ProjectBase
from origin.views.public.mcp.progress.get_progress_history import _run as progress_history_run

PR = "https://github.com/acme/widgets/pull/7"


class Base(ProjectBase):
    def setUp(self):
        super().setUp()
        self.mine = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Mine",
            reporter=self.user,
            assignee=self.user,
        )
        self.foreign_deleted = TaskMaster.objects.create(
            team=self.other_team,
            project=self.foreign,
            title="Gone",
            reporter=self.foreign.owner,
            assignee=self.foreign.owner,
            is_deleted=True,
        )
        self.foreign_task_id = self.foreign_task.task_id
        self.foreign_milestone_id = self.foreign_milestone.milestone_id


class TaskToolsAnswerAlike(Base):
    def task_tool_cases(self):
        me = str(self.user.id)
        yield (
            "add_comment",
            lambda tid: add_comment_run({"task_id": tid, "body_text": "x"}, self.ctx),
        )
        yield (
            "add_task_delivery",
            lambda tid: add_delivery_run({"task_id": tid, "pr_url": PR}, self.ctx),
        )
        yield (
            "remove_task_delivery",
            lambda tid: remove_delivery_run({"task_id": tid, "pr_url": PR}, self.ctx),
        )
        yield (
            "assign_task",
            lambda tid: assign_task_run({"task_id": tid, "assignee_id": me}, self.ctx),
        )
        yield "fetch_task", lambda tid: fetch_task_run({"task_id": tid}, self.ctx)
        yield "get_task_blockers", lambda tid: task_blockers_run({"task_id": tid}, self.ctx)
        yield "get_task_history", lambda tid: task_history_run({"task_id": tid}, self.ctx)
        yield "get_progress_history", lambda tid: progress_history_run({"task_id": tid}, self.ctx)
        yield "update_task", lambda tid: update_task_run({"task_id": tid, "title": "x"}, self.ctx)
        # the shared loader of update_comment / delete_comment / restore_task_body
        yield "task_access.load_task", lambda tid: task_access.load_task(tid, self.ctx, action="x")

    def test_a_task_in_another_team_reads_like_a_missing_one(self):
        for name, call in self.task_tool_cases():
            with self.subTest(tool=name):
                missing = self.refusal(call, MISSING)
                foreign = self.refusal(call, self.foreign_task_id)
                self.assertEqual(str(missing), f"Task {MISSING} not found.")
                self.assert_same_words(missing, foreign, f"Task {self.foreign_task_id} not found.")

    def test_a_deleted_task_in_another_team_reads_like_a_missing_one_too(self):
        # "has been deleted." used to come before the team check: a second oracle, on tasks
        # that were deleted in someone else's team.
        for name, call in self.task_tool_cases():
            if name in ("get_task_history", "get_task_blockers"):
                continue  # these never had a "deleted" answer (history of a deleted task is read)
            with self.subTest(tool=name):
                missing = self.refusal(call, MISSING)
                foreign = self.refusal(call, self.foreign_deleted.task_id)
                self.assert_same_words(
                    missing, foreign, f"Task {self.foreign_deleted.task_id} not found."
                )
                self.assertIsInstance(foreign, ToolForbidden)

    def test_the_class_still_tells_the_refusal_alarm_which_it_was(self):
        for name, call in self.task_tool_cases():
            with self.subTest(tool=name):
                self.assertNotIsInstance(self.refusal(call, MISSING), ToolForbidden)
                self.assertIsInstance(self.refusal(call, self.foreign_task_id), ToolForbidden)

    def test_a_deleted_task_of_my_own_team_keeps_its_own_answer(self):
        self.mine.is_deleted = True
        self.mine.save(update_fields=["is_deleted"])
        for name, call in self.task_tool_cases():
            if name in ("get_task_history", "get_task_blockers"):
                continue
            with self.subTest(tool=name):
                self.assertEqual(
                    str(self.refusal(call, self.mine.task_id)),
                    f"Task {self.mine.task_id} has been deleted.",
                )

    def test_a_teammate_outside_the_project_keeps_the_answer_for_that(self):
        # unchanged on purpose: they are in the team, so the task is no secret from them
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolForbidden) as raised:
            fetch_task_run({"task_id": self.mine.task_id}, ctx)
        self.assertIn(f"Not authorized to read task {self.mine.task_id}", str(raised.exception))

    def test_a_member_still_reads_and_writes_the_task(self):
        out = fetch_task_run({"task_id": self.mine.task_id}, self.ctx)
        self.assertEqual(out["task_id"], self.mine.task_id)
        add_comment_run({"task_id": self.mine.task_id, "body_text": "hello"}, self.ctx)
        self.assertTrue(TaskComments.objects.filter(task=self.mine.task_id).exists())


class BulkUpdateAnswersAlike(Base):
    def message(self, tid):
        try:
            update_bulk_run({"updates": [{"task_id": tid, "title": "x"}]}, self.ctx)
        except Exception as e:  # noqa: BLE001
            return e
        self.fail("expected a refusal")

    def test_a_task_in_another_team_reads_like_a_missing_one(self):
        missing = self.message(MISSING)
        self.assertIn(f"updates[0]: task {MISSING} not found.", str(missing))
        for tid in (self.foreign_task_id, self.foreign_deleted.task_id):
            with self.subTest(task=tid):
                foreign = self.message(tid)
                self.assertEqual(shape(foreign), shape(missing))
                self.assertIn(f"updates[0]: task {tid} not found.", str(foreign))
                self.assertNotIn("different team", str(foreign))


class MilestoneToolsAnswerAlike(Base):
    def milestone_tool_cases(self):
        yield (
            "get_milestone_summary",
            lambda mid: milestone_summary_run({"milestone_id": mid}, self.ctx),
        )
        yield "list_tasks", lambda mid: list_tasks_run({"milestone_id": mid}, self.ctx)
        yield (
            "list_task_dependencies",
            lambda mid: dependencies_run({"milestone_id": mid}, self.ctx),
        )
        yield (
            "create_task_plan(existing_milestone_id)",
            lambda mid: create_task_plan_run(
                {
                    "project_id": self.project.project_id,
                    "existing_milestone_id": mid,
                    "tasks": [{"title": "a"}],
                },
                self.ctx,
            ),
        )

    def test_a_milestone_in_another_team_reads_like_a_missing_one(self):
        for name, call in self.milestone_tool_cases():
            with self.subTest(tool=name):
                missing = self.refusal(call, MISSING)
                foreign = self.refusal(call, self.foreign_milestone_id)
                self.assertEqual(str(missing), f"Milestone {MISSING} not found.")
                self.assert_same_words(
                    missing, foreign, f"Milestone {self.foreign_milestone_id} not found."
                )
                self.assertNotIsInstance(missing, ToolForbidden)
                self.assertIsInstance(foreign, ToolForbidden)

    def test_the_milestone_move_says_the_same(self):
        missing = milestone_move_error(self.mine, MISSING, self.team.team_id)
        foreign = milestone_move_error(self.mine, self.foreign_milestone_id, self.team.team_id)
        self.assertEqual(missing, f"Milestone {MISSING} not found.")
        self.assert_same_words(
            missing, foreign, f"Milestone {self.foreign_milestone_id} not found."
        )

    def test_update_task_filing_under_a_milestone_in_another_team(self):
        def call(mid):
            return update_task_run({"task_id": self.mine.task_id, "milestone_id": mid}, self.ctx)

        missing = self.refusal(call, MISSING)
        foreign = self.refusal(call, self.foreign_milestone_id)
        self.assert_same_words(
            missing, foreign, f"Milestone {self.foreign_milestone_id} not found."
        )

    def test_a_milestone_of_my_own_team_still_resolves(self):
        mine = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", reporter=self.user
        )
        self.assertIsNone(milestone_move_error(self.mine, mine.milestone_id, self.team.team_id))
        out = milestone_summary_run({"milestone_id": mine.milestone_id}, self.ctx)
        self.assertTrue(out)


class CreateTaskPlanAnswersAlike(Base):
    def plan(self, **extra):
        args = {"project_id": self.project.project_id, "tasks": [{"title": "a"}], **extra}
        return lambda value: create_task_plan_run({**args, **{k: value for k in extra}}, self.ctx)

    def test_the_project_reads_like_a_missing_one(self):
        def call(pid):
            return create_task_plan_run({"project_id": pid, "tasks": [{"title": "a"}]}, self.ctx)

        missing = self.refusal(call, MISSING)
        foreign = self.refusal(call, self.foreign.project_id)
        self.assertEqual(str(missing), f"Project {MISSING} not found.")
        self.assert_same_words(missing, foreign, f"Project {self.foreign.project_id} not found.")
        self.assertNotIsInstance(missing, ToolForbidden)
        self.assertIsInstance(foreign, ToolForbidden)

    def test_the_parent_task_reads_like_a_missing_one(self):
        call = self.plan(parent_task_id=0)
        missing = self.refusal(call, MISSING)
        self.assertEqual(str(missing), f"Task {MISSING} not found.")
        for tid in (self.foreign_task_id, self.foreign_deleted.task_id):
            with self.subTest(task=tid):
                foreign = self.refusal(call, tid)
                self.assert_same_words(missing, foreign, f"Task {tid} not found.")
                self.assertIsInstance(foreign, ToolForbidden)
        self.assertNotIsInstance(missing, ToolForbidden)

    def test_nothing_was_created(self):
        before = TaskMaster.objects.count()
        self.refusal(self.plan(parent_task_id=0), self.foreign_task_id)
        self.refusal(self.plan(existing_milestone_id=0), self.foreign_milestone_id)
        self.assertEqual(TaskMaster.objects.count(), before)


class SprintLoaderAnswersAlike(Base):
    def test_a_deleted_sprint_in_another_team_reads_like_a_missing_one(self):
        # "has been deleted." came before the team check, as it did for tasks
        Sprint.objects.filter(pk=self.foreign_sprint.pk).update(is_deleted=True)
        missing = self.refusal(sprint_access.load_sprint, MISSING, self.ctx)
        foreign = self.refusal(sprint_access.load_sprint, self.foreign_sprint.sprint_id, self.ctx)
        self.assert_same_words(
            missing,
            foreign,
            f"Sprint {self.foreign_sprint.sprint_id} not found. Get ids from `list_sprints`.",
        )
        self.assertIsInstance(foreign, ToolForbidden)

    def test_a_deleted_sprint_of_my_own_team_keeps_its_own_answer(self):
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S",
            sequence_number=1,
            start_date="2026-08-01",
            end_date="2026-08-14",
            is_deleted=True,
        )
        self.assertIn(
            "has been deleted",
            str(self.refusal(sprint_access.load_sprint, sprint.sprint_id, self.ctx)),
        )

"""A task of a soft-deleted project reads like a missing task, in every single-task read and in search
(MIP-1590 item 5, flag `AGENT_PARITY_FOLLOWUPS`; the follow-up of the first paid run).

`ProjectMaster.is_deleted` marks a project that is gone. With the flag on `fetch_task` (already), `get_task_history`,
`get_task_blockers` and `search_knowledge_base` answer as they do for a task that was never there: the same class,
the same words, no hit. A member of the project, a teammate who is not on it and a stranger all get the one answer
(the oracle of "this task existed" is closed). With the flag off nothing moves.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.parity import FOLLOWUPS_FLAG
from origin.search_engine.agent.tools import ToolContext, ToolError, project_gone
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.get_task_blockers import GET_TASK_BLOCKERS
from origin.search_engine.agent.tools.get_task_history import GET_TASK_HISTORY
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE

from .test_mcp_endpoint import McpBase

ON = {FOLLOWUPS_FLAG: True}
OFF = {FOLLOWUPS_FLAG: False}


class Base(McpBase):
    flags = ON

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, self.flags)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.gone = ProjectMaster.objects.create(
            team=self.team,
            project_name="Wound-down project",
            code="WND",
            owner=self.user,
            project_system_user=self.user,
            is_deleted=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.gone, attendee=self.user)
        self.gone_task = TaskMaster.objects.create(
            team=self.team,
            project=self.gone,
            reporter=self.user,
            title="Renew the harbour lease",
            status="Open",
            project_task_number=1,
        )
        self.missing_id = 9_999_999

    def words(self, exc, task_id):
        return str(exc).replace(str(task_id), "N")


class SingleTaskReadTests(Base):
    TOOLS = (
        ("fetch_task", FETCH_TASK),
        ("get_task_history", GET_TASK_HISTORY),
        ("get_task_blockers", GET_TASK_BLOCKERS),
    )

    def answer(self, tool, task_id, ctx=None):
        try:
            tool.run({"task_id": task_id}, ctx or self.ctx)
        except ToolError as exc:
            return exc
        return None

    def test_a_task_of_a_deleted_project_reads_exactly_like_a_missing_task(self):
        for name, tool in self.TOOLS:
            gone = self.answer(tool, self.gone_task.task_id)
            missing = self.answer(tool, self.missing_id)
            self.assertIsNotNone(gone, name)
            self.assertIs(type(gone), type(missing), name)
            self.assertEqual(
                self.words(gone, self.gone_task.task_id), self.words(missing, self.missing_id), name
            )

    def test_a_teammate_who_is_not_on_the_project_gets_the_same_answer_not_a_refusal(self):
        # Before: "Not authorized" for the history of a task that exists, "not found" for one that does not.
        for name, tool in self.TOOLS:
            gone = self.answer(tool, self.gone_task.task_id, self.ctx2)
            missing = self.answer(tool, self.missing_id, self.ctx2)
            self.assertIs(type(gone), type(missing), name)
            self.assertEqual(
                self.words(gone, self.gone_task.task_id), self.words(missing, self.missing_id), name
            )

    def test_the_same_number_of_queries_as_a_missing_task_or_fewer(self):
        for name, tool in self.TOOLS:
            with CaptureQueriesContext(connection) as gone:
                self.answer(tool, self.gone_task.task_id)
            with CaptureQueriesContext(connection) as missing:
                self.answer(tool, self.missing_id)
            self.assertLessEqual(len(gone), len(missing) + 2, name)

    def test_the_flag_off_reads_it_as_it_always_did(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIsNone(self.answer(GET_TASK_HISTORY, self.gone_task.task_id))
            self.assertIsNone(self.answer(GET_TASK_BLOCKERS, self.gone_task.task_id))
            self.assertIsNone(self.answer(FETCH_TASK, self.gone_task.task_id))

    def test_a_task_of_a_live_project_is_still_read_with_the_flag_on(self):
        live = ProjectMaster.objects.create(
            team=self.team,
            project_name="Live",
            code="LIV",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=live, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team,
            project=live,
            reporter=self.user,
            title="Live",
            status="Open",
            project_task_number=1,
        )
        for _name, tool in self.TOOLS:
            self.assertIsNone(self.answer(tool, task.task_id))

    def test_no_title_or_project_name_reaches_the_answer_or_the_log(self):
        with self.assertNoLogs(level="INFO"):
            exc = self.answer(GET_TASK_HISTORY, self.gone_task.task_id)
        self.assertNotIn("harbour", str(exc))
        self.assertNotIn("Wound", str(exc))


class SearchTests(Base):
    def hits(self, *project_ids):
        return {
            "results": [
                {
                    "entity_type": "task",
                    "entity_id": str(i),
                    "title": f"hit {i}",
                    "snippet": "s",
                    "chunks": [],
                    "task_id": i,
                    "project_id": pid,
                }
                for i, pid in enumerate(project_ids, start=1)
            ]
        }

    def search(self, *project_ids):
        with mock.patch(
            "origin.search_engine.agent.tools.search_kb.search",
            return_value=self.hits(*project_ids),
        ):
            return SEARCH_KNOWLEDGE_BASE.run({"query": "harbour lease"}, self.ctx)["matches"]

    def test_the_hits_of_a_deleted_project_are_dropped_and_the_others_kept(self):
        live = ProjectMaster.objects.create(
            team=self.team,
            project_name="Live",
            code="LIV",
            owner=self.user,
            project_system_user=self.user,
        )
        matches = self.search(self.gone.project_id, live.project_id, None)
        self.assertEqual([m["title"] for m in matches], ["hit 2", "hit 3"])

    def test_the_flag_off_keeps_every_hit(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertEqual(len(self.search(self.gone.project_id, None)), 2)

    def test_one_query_for_all_the_hits_and_none_when_no_hit_names_a_project(self):
        with CaptureQueriesContext(connection) as queries:
            self.search(self.gone.project_id, self.gone.project_id, self.gone.project_id)
        gone_queries = [q for q in queries if "origin_projectmaster" in q["sql"]]
        self.assertEqual(len(gone_queries), 1)
        with CaptureQueriesContext(connection) as queries:
            self.search(None, None)
        self.assertEqual([q for q in queries if "origin_projectmaster" in q["sql"]], [])

    def test_the_helper_is_a_no_op_with_the_flag_off(self):
        matches = [{"project_id": self.gone.project_id}]
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIs(project_gone.drop_deleted_projects(matches), matches)

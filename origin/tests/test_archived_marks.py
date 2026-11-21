"""Archived projects and chats are marked, not hidden (MIP-1852).

Search, the agent's task and chat lists and the MCP tools keep returning items in an archived
project or group chat, ranked as before, with `archived: true` on them. A live item has no key.
No new work is created in an archived project.
"""

from unittest import mock

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.list_my_chats import LIST_MY_CHATS
from origin.search_engine.agent.tools.list_projects import LIST_PROJECTS
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.services.archive import mark_archived_hits
from origin.views.public.mcp.list_paging import run_list_projects

from .test_base import BaseAPITestCase


class _Case(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.live = self._project("Live", "LIV")
        self.shut = self._project("Shut", "SHT", archived=True)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _project(self, name, code, archived=False):
        p = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            archived_at=timezone.now() if archived else None,
        )
        ProjectMembers.objects.create(team=self.team, project=p, attendee=self.user)
        return p

    def _task(self, project, title):
        return TaskMaster.objects.create(
            team=self.team, project=project, title=title, status="Open", assignee=self.user
        )

    def _gm(self, title, archived=False, project=None, kind=ChannelKind.GM):
        c = Channel.objects.create(
            team=self.team,
            kind=kind,
            title=title,
            owner=self.user,
            is_private=True,
            project=project,
            archived_at=timezone.now() if archived else None,
        )
        ChannelMember.objects.create(channel=c, user=self.user)
        return c


class ListMarkTests(_Case):
    def test_list_projects_marks_only_the_archived_one(self):
        rows = {r["project_name"]: r for r in LIST_PROJECTS.run({}, self.ctx)["projects"]}
        self.assertIs(rows["Shut"]["archived"], True)
        self.assertNotIn("archived", rows["Live"])

    def test_mcp_list_projects_marks_it_too(self):
        rows = {r["project_name"]: r for r in run_list_projects({}, self.ctx)["projects"]}
        self.assertIs(rows["Shut"]["archived"], True)
        self.assertNotIn("archived", rows["Live"])

    def test_list_tasks_and_fetch_task_mark_a_task_in_an_archived_project(self):
        live, shut = self._task(self.live, "a"), self._task(self.shut, "b")
        rows = {r["task_id"]: r for r in LIST_TASKS.run({}, self.ctx)["tasks"]}
        self.assertIs(rows[shut.task_id]["archived"], True)
        self.assertNotIn("archived", rows[live.task_id])
        self.assertIs(FETCH_TASK.run({"task_id": shut.task_id}, self.ctx)["archived"], True)
        self.assertNotIn("archived", FETCH_TASK.run({"task_id": live.task_id}, self.ctx))

    def test_list_tasks_costs_the_same_queries_with_an_archived_project(self):
        self._task(self.live, "a")
        with self.assertNumQueries(self._count(LIST_TASKS)):
            LIST_TASKS.run({}, self.ctx)
        self._task(self.shut, "b")
        # one more task row, no more queries: the project is already joined
        with self.assertNumQueries(self._count(LIST_TASKS)):
            LIST_TASKS.run({}, self.ctx)

    def _count(self, tool):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as q:
            tool.run({}, self.ctx)
        return len(q)

    def test_list_my_chats_marks_archived_chats_and_chats_of_archived_projects(self):
        self._gm("open")
        self._gm("shut", archived=True)
        pm_shut = Channel.objects.get(project=self.shut, kind=ChannelKind.PM)
        pm_live = Channel.objects.get(project=self.live, kind=ChannelKind.PM)
        for pm in (pm_shut, pm_live):
            ChannelMember.objects.get_or_create(channel=pm, user=self.user)
        rows = {r["chat_id"]: r for r in LIST_MY_CHATS.run({}, self.ctx)["chats"]}
        titled = {r["title"]: r for r in rows.values()}
        self.assertIs(titled["shut"]["archived"], True)
        self.assertIs(rows[str(pm_shut.id)]["archived"], True)
        self.assertNotIn("archived", titled["open"])
        self.assertNotIn("archived", rows[str(pm_live.id)])


class SearchMarkTests(_Case):
    def _hits(self):
        gm = self._gm("shut chat", archived=True)
        return [
            {"entity_type": "task", "entity_id": "1", "project_id": str(self.live.project_id)},
            {"entity_type": "task", "entity_id": "2", "project_id": str(self.shut.project_id)},
            {"entity_type": "chat", "entity_id": "3", "chat_id": str(gm.id)},
            {"entity_type": "chat", "entity_id": "4", "chat_id": "not-a-uuid"},
            {"entity_type": "note", "entity_id": "5", "project_id": None},
        ]

    def test_mark_archived_hits_marks_in_place_and_keeps_order(self):
        hits = self._hits()
        order = [h["entity_id"] for h in hits]
        with self.assertNumQueries(2):
            mark_archived_hits(hits)
        self.assertEqual([h["entity_id"] for h in hits], order)
        self.assertEqual([bool(h.get("archived")) for h in hits], [False, True, True, False, False])

    def test_no_project_or_chat_means_no_query(self):
        with self.assertNumQueries(0):
            mark_archived_hits([{"entity_type": "todo", "entity_id": "9"}])

    def test_the_agent_search_tool_marks_its_matches(self):
        hits = [
            {
                "entity_type": "task",
                "entity_id": str(i),
                "title": "t",
                "snippet": "s",
                "chunks": [],
                "project_id": str(p.project_id),
            }
            for i, p in ((1, self.live), (2, self.shut))
        ]
        with mock.patch(
            "origin.search_engine.agent.tools.search_kb.search",
            return_value={"query": "q", "results": hits},
        ):
            out = SEARCH_KNOWLEDGE_BASE.run({"query": "q"}, self.ctx)
        marks = [bool(m.get("archived")) for m in out["matches"]]
        self.assertEqual(marks, [False, True])
        self.assertEqual([m["entity_id"] for m in out["matches"]], ["1", "2"])

    def test_the_search_endpoint_marks_its_results(self):
        self.authenticate(self.user)
        hits = [
            {"entity_type": "task", "entity_id": "2", "project_id": str(self.shut.project_id)},
            {"entity_type": "task", "entity_id": "1", "project_id": str(self.live.project_id)},
        ]
        with mock.patch(
            "origin.search_engine.views.search", return_value={"query": "q", "results": hits}
        ):
            res = self.client.post(
                "/api/v2/search/", {"query": "q", "team_id": str(self.team.team_id)}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([bool(r.get("archived")) for r in res.data["results"]], [True, False])


class NoNewWorkTests(_Case):
    def test_create_task_refuses_an_archived_project_and_creates_nothing(self):
        before = TaskMaster.objects.count()
        with self.assertRaises(ToolError) as cm:
            CREATE_TASK.run({"title": "T", "project_id": self.shut.project_id}, self.ctx)
        self.assertIn("archived", str(cm.exception))
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_a_live_project_still_takes_a_task(self):
        out = CREATE_TASK.run({"title": "T", "project_id": self.live.project_id}, self.ctx)
        self.assertTrue(out["task_id"])

    def test_a_non_member_is_refused_before_the_archive_is_mentioned(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolError) as cm:
            CREATE_TASK.run({"title": "T", "project_id": self.shut.project_id}, ctx)
        self.assertNotIn("archived", str(cm.exception))

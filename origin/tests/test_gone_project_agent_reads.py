"""A task of a soft-deleted project is not reachable through the agent or MCP (MIP-1621, the agent half).

api #909/#912 made `fetch_task`, the mention resolver, `search_knowledge_base`, `get_task_history` and
`get_task_blockers` answer such a task as a MISSING one (flag `AGENT_PARITY_FOLLOWUPS`). A paid eval
(`t1590.deleted-project-task`) showed the model still reaching it through `list_tasks` (the assignee and
reporter branches of the ACL), then through the timeline, and the write tools that load a task by id still
changed it. This module pins every path the inventory found, per kind of caller (the project's member and
owner, an assignee who is not on the project, a teammate with no part in it, a stranger of another team):

  * a LIST or READ never carries the task: its id, display id, title or project name appear nowhere in
    what the tool returns, counts and totals are computed on the live set;
  * a BY-ID call answers EXACTLY as a missing id (class, words, rows written);
  * flag OFF is what the tools did before: the leak the model saw is pinned too (the twin), so the flag is
    the only difference;
  * the live project's twin keeps answering what it answered.
"""

from __future__ import annotations

import json
import logging
import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.search_engine.agent.evals.group import seeds
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.parity import FOLLOWUPS_FLAG
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError, project_gone
from origin.services import item_lines
from origin.tests.fixtures.sequence_scenario import SequenceScenario
from origin.tests.test_base import BaseAPITestCase

from .test_mcp_endpoint import McpBase

User = get_user_model()
ON = {FOLLOWUPS_FLAG: True}
OFF = {FOLLOWUPS_FLAG: False}

GONE_TITLE = "Renew the harbour lease"
GONE_PROJECT = "Wound-down harbour"
LIVE_TITLE = "Repaint the lighthouse"
MISSING = 987_654_321


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        if not record.name.startswith("django.db"):  # the query log the capture itself switches on
            self.lines.append(
                re.sub(r"\d+", "N", f"{record.name} {record.levelname} {record.getMessage()}")
            )


def observed(fn):
    """`(result, number of queries, log lines)` of `fn()`: the three things a caller can tell a gone task from a
    missing one by, besides the answer itself."""
    handler, root = _Records(), logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        with CaptureQueriesContext(connection) as queries:
            out = fn()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    return out, len(queries), handler.lines


class World(McpBase):
    """A gone project and a live twin; every kind of caller.

    self.user    owner of both projects, on both (a member row), reporter of every task
    self.user2   a member of both projects
    self.holder  the ASSIGNEE of every task, on the team, NOT on either project
    self.mate    on the team, nothing to do with either project
    self.alien   on another team altogether
    """

    flags = ON

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, self.flags)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.holder = User.objects.create_user(
            username="holder", email="holder@x.com", password="p"
        )
        self.mate = User.objects.create_user(username="mate", email="mate@x.com", password="p")
        self.alien = User.objects.create_user(username="alien", email="alien@x.com", password="p")
        for person in (self.holder, self.mate):
            TeamMembers.objects.create(team=self.team, attendee=person)
        self.alien_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@x.com", owner=self.alien
        )
        TeamMembers.objects.create(team=self.alien_team, attendee=self.alien)

        self.gone = self.make_project("GON", GONE_PROJECT, is_deleted=True)
        self.live = self.make_project("LIV", "Lighthouse", is_deleted=False)
        self.gone_task = self.make_task(self.gone, GONE_TITLE)
        self.gone_child = self.make_task(
            self.gone, "Sub task of the lease", 2, parent_task_id=self.gone_task.task_id
        )
        self.live_task = self.make_task(self.live, LIVE_TITLE)
        self.gone_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.gone, title="Harbour milestone", reporter=self.user
        )
        self.live_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.live, title="Lighthouse milestone", reporter=self.user
        )
        TaskMaster.objects.filter(pk=self.gone_task.pk).update(milestone=self.gone_ms)
        TaskMaster.objects.filter(pk=self.live_task.pk).update(milestone=self.live_ms)
        self.gone_sprint = Sprint.objects.create(
            team=self.team,
            project=self.gone,
            name="Harbour sprint",
            sequence_number=1,
            start_date="2026-11-01",
            end_date="2026-11-30",
            status="active",
        )
        TaskMaster.objects.filter(pk=self.gone_task.pk).update(sprint=self.gone_sprint)
        # An edge between the two projects, seen from the live end.
        TaskDependency.objects.create(
            blocker_task=self.gone_task,
            blocked_task=self.live_task,
            team=self.team,
            created_by=self.user,
        )
        TaskComments.objects.create(
            task=self.gone_task,
            comment_id=1,
            sender=self.user,
            comment_body=[{"type": "paragraph"}],
        )
        TaskDelivery.objects.create(
            team=self.team,
            task=self.gone_task,
            repo_owner="acme",
            repo_name="rocket",
            branch="feature/lease-renewal",
            pr_number=7,
            pr_url="https://github.com/acme/rocket/pull/7",
            state="open",
            source="manual",
            created_by=self.user,
        )

    # -- seeds --

    def make_project(self, code, name, *, is_deleted):
        row = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            is_deleted=is_deleted,
        )
        for person in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=row, attendee=person)
        return row

    def make_task(self, project, title, number=1, **extra):
        fields = {
            "team": self.team,
            "project": project,
            "title": title,
            "status": "Open",
            "assignee": self.holder,
            "reporter": self.user,
            "project_task_number": number,
            "due_date": "2026-11-20",
        }
        fields.update(extra)
        return TaskMaster.objects.create(**fields)

    @property
    def people(self):
        return {
            "owner": (self.user, self.team),
            "member": (self.user2, self.team),
            "assignee not on the project": (self.holder, self.team),
            "teammate": (self.mate, self.team),
        }

    def ctx(self, person, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(person.id))

    def run_tool(self, name, args, person, team=None):
        """`(result, None)` or `(None, exception)`; the registry tool, as the in-app agent calls it."""
        try:
            return REGISTRY[name].run(dict(args), self.ctx(person, team)), None
        except ToolError as exc:
            return None, exc

    def asks(self, name, args, person, team=None):
        result, exc = self.run_tool(name, args, person, team)
        return json.dumps(result if exc is None else str(exc), default=str)


MARKERS = (GONE_TITLE, GONE_PROJECT, "GON-1", "GON-2", "Sub task of the lease", "Harbour milestone")


def has_marker(text):
    return [m for m in MARKERS if m in text]


class ListAndReadTests(World):
    """Each row: tool, arguments. The result must not carry the gone project's task (flag on); with the flag
    off the ones marked LEAKS_TODAY are pinned as they were."""

    def calls(self):
        gid, lid = self.gone.project_id, self.live.project_id
        return {
            "list_tasks": ({}, True),
            "list_tasks assignee": ({"assignee_id": str(self.holder.id)}, True),
            "list_tasks milestone": ({"milestone_id": self.gone_ms.milestone_id}, False),
            "list_tasks parent": ({"parent_task_id": self.gone_task.task_id}, True),
            "list_tasks sprint": ({"sprint_id": self.gone_sprint.sprint_id}, False),
            "list_tasks project": ({"project_id": gid}, False),
            "list_tasks live project": ({"project_id": lid}, False),
            "get_my_focus_tasks": ({}, False),
            "get_stale_tasks": ({}, False),
            "get_my_task_summary": ({}, False),
            "get_team_task_summary": ({}, False),
            "get_project_summary": ({"project_id": gid}, False),
            "get_milestone_summary": ({"milestone_id": self.gone_ms.milestone_id}, True),
            "get_sprint_summary": ({"sprint_id": self.gone_sprint.sprint_id}, True),
            "list_milestones": ({"project_id": gid}, False),
            "list_my_milestones": ({}, False),
            "list_sprints": ({"project_id": gid}, False),
            "list_task_dependencies live end": ({"project_id": lid}, True),
            "get_my_blockers": ({}, False),
            "get_task_blockers live end": ({"task_id": self.live_task.task_id}, False),
            "get_workload_distribution": ({}, False),
            "get_my_throughput": ({}, False),
            "get_task_throughput_stats": ({}, False),
            "get_top_task_closers": ({}, False),
            "get_project_activity_ranking": ({}, False),
            "get_my_schedule": ({}, False),
            "find_task_by_delivery branch": ({"branch": "feature/lease-renewal"}, True),
            "fetch_task child": ({"task_id": self.gone_child.task_id}, False),
            "fetch_task live": ({"task_id": self.live_task.task_id}, False),
        }

    def tool_of(self, label):
        return label.split(" ")[0]

    def test_nothing_of_the_gone_project_comes_back_with_the_flag_on(self):
        for who, (person, team) in self.people.items():
            for label, (args, _leaks) in self.calls().items():
                with self.subTest(f"{label} as {who}"):
                    self.assertEqual(
                        has_marker(self.asks(self.tool_of(label), args, person, team)), []
                    )

    def test_the_flag_off_is_what_the_model_saw_before(self):
        """The twin: every call marked as a leak still leaks with the flag off, so the flag is the only
        difference between the two states."""
        for label, (args, leaks) in self.calls().items():
            if not leaks:
                continue
            with self.subTest(label):
                with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
                    text = self.asks(self.tool_of(label), args, self.user)
                self.assertTrue(has_marker(text), f"{label}: {text[:300]}")

    def test_list_tasks_leaked_through_the_assignee_branch_and_no_longer_does(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            before = self.asks("list_tasks", {}, self.holder)
        self.assertIn(GONE_TITLE, before)
        after = self.asks("list_tasks", {}, self.holder)
        self.assertNotIn(GONE_TITLE, after)
        self.assertIn(LIVE_TITLE, after)

    def test_the_page_and_has_more_are_computed_on_the_live_set(self):
        # The assignee holds one live task; the owner two (the base fixture's WRD task and the live twin's).
        for person, live_titles in (
            (self.holder, [LIVE_TITLE]),
            (self.user, [LIVE_TITLE, self.task.title]),
        ):
            result, exc = self.run_tool("list_tasks", {"limit": len(live_titles)}, person)
            self.assertIsNone(exc)
            self.assertCountEqual([t["title"] for t in result["tasks"]], live_titles)
            # A page exactly as long as the live set: nothing more, and no cursor that leads to the gone ones.
            self.assertFalse(result["has_more"], person.username)
            self.assertNotIn("next_cursor", result)
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            result, _ = self.run_tool("list_tasks", {"limit": 1}, self.holder)
        self.assertTrue(result["has_more"])

    def test_the_live_projects_rows_are_the_rows_they_were(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            off, _ = self.run_tool("list_tasks", {"project_id": self.live.project_id}, self.user)
        on, _ = self.run_tool("list_tasks", {"project_id": self.live.project_id}, self.user)
        self.assertEqual(
            json.dumps(off, sort_keys=True, default=str),
            json.dumps(on, sort_keys=True, default=str),
        )

    def test_the_gone_projects_tool_answers_equal_a_missing_one(self):
        for who, (person, team) in self.people.items():
            for name, key in (
                ("get_milestone_summary", "milestone_id"),
                ("get_sprint_summary", "sprint_id"),
            ):
                gone_id = (
                    self.gone_ms.milestone_id
                    if key == "milestone_id"
                    else self.gone_sprint.sprint_id
                )
                with self.subTest(f"{name} as {who}"):
                    _, gone = self.run_tool(name, {key: gone_id}, person, team)
                    _, missing = self.run_tool(name, {key: MISSING}, person, team)
                    self.assertIsNotNone(gone)
                    self.assertIs(type(gone), type(missing))
                    self.assertEqual(
                        re.sub(r"\d+", "N", str(gone)), re.sub(r"\d+", "N", str(missing))
                    )

    def test_the_parent_filter_answers_a_gone_parent_as_a_missing_one(self):
        for who, (person, team) in self.people.items():
            with self.subTest(who):
                _, gone = self.run_tool(
                    "list_tasks", {"parent_task_id": self.gone_task.task_id}, person, team
                )
                _, missing = self.run_tool("list_tasks", {"parent_task_id": MISSING}, person, team)
                self.assertIs(type(gone), type(missing))
                self.assertEqual(re.sub(r"\d+", "N", str(gone)), re.sub(r"\d+", "N", str(missing)))

    def test_one_extra_join_and_no_extra_query(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            with CaptureQueriesContext(connection) as off:
                self.run_tool("list_tasks", {}, self.holder)
        with CaptureQueriesContext(connection) as on:
            self.run_tool("list_tasks", {}, self.holder)
        self.assertEqual(len(on), len(off))


class ByIdWriteTests(World):
    """Every tool that loads a task by id answers a gone project's task as a missing id, and writes nothing."""

    def calls(self):
        def args(**extra):
            return lambda t: dict(task_id=t, **extra)

        return {
            "add_comment": ("add_comment", args(body_text="hello")),
            "assign_task": ("assign_task", args(assignee_id=str(self.user2.id))),
            "update_task": ("update_task", args(title="renamed")),
            "update_comment": ("update_comment", args(comment_id=1, body_text="edited")),
            "delete_comment": ("delete_comment", args(comment_id=1)),
            "restore_task_body": ("restore_task_body", args(activity_id=1)),
            "add_task_delivery": (
                "add_task_delivery",
                args(pr_url="https://github.com/acme/rocket/pull/9"),
            ),
            "remove_task_delivery": (
                "remove_task_delivery",
                args(pr_url="https://github.com/acme/rocket/pull/7"),
            ),
            "get_task_history": ("get_task_history", args()),
            "get_task_blockers": ("get_task_blockers", args()),
            "fetch_task": ("fetch_task", args()),
        }

    def call_exc(self, name, build, task_id, person, team=None):
        result, exc = self.run_tool(name, build(task_id), person, team)
        return exc

    def norm(self, exc, task_id):
        return None if exc is None else re.sub(r"\d+", "N", str(exc).replace(str(task_id), "N"))

    def test_a_gone_projects_task_answers_exactly_as_a_missing_id(self):
        before = TaskMaster.objects.filter(pk=self.gone_task.pk).values().first()
        comments = TaskComments.objects.count()
        for who, (person, team) in self.people.items():
            for label, (name, build) in self.calls().items():
                with self.subTest(f"{label} as {who}"):
                    gone, gone_queries, gone_logs = observed(
                        lambda: self.call_exc(name, build, self.gone_task.task_id, person, team)
                    )
                    missing, missing_queries, missing_logs = observed(
                        lambda: self.call_exc(name, build, MISSING, person, team)
                    )
                    self.assertIsNotNone(gone)
                    self.assertIs(type(gone), type(missing))
                    self.assertEqual(
                        self.norm(gone, self.gone_task.task_id), self.norm(missing, MISSING)
                    )
                    # The same query class (the refusal takes the lookup and nothing more) and the same
                    # log lines (none that says which one it was).
                    self.assertLessEqual(gone_queries, missing_queries + 2)
                    self.assertEqual(gone_logs, missing_logs)
        self.assertEqual(TaskMaster.objects.filter(pk=self.gone_task.pk).values().first(), before)
        self.assertEqual(TaskComments.objects.count(), comments)

    def test_a_stranger_of_another_team_gets_the_same_answer_too(self):
        for label, (name, build) in self.calls().items():
            with self.subTest(label):
                gone = self.call_exc(
                    name, build, self.gone_task.task_id, self.alien, self.alien_team
                )
                missing = self.call_exc(name, build, MISSING, self.alien, self.alien_team)
                # The words only: a task of another team is a `ToolForbidden` with a missing task's words
                # (MIP-1531), so the refusal alarm still counts the probe.
                self.assertEqual(
                    self.norm(gone, self.gone_task.task_id), self.norm(missing, MISSING)
                )

    def test_the_flag_off_still_loads_it_as_it_always_did(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            result, exc = self.run_tool(
                "add_comment", {"task_id": self.gone_task.task_id, "body_text": "hi"}, self.holder
            )
        self.assertIsNone(exc)
        self.assertTrue(
            TaskComments.objects.filter(task=self.gone_task, comment_body__isnull=False).exists()
        )

    def test_the_live_projects_task_is_still_written(self):
        result, exc = self.run_tool(
            "add_comment", {"task_id": self.live_task.task_id, "body_text": "hi"}, self.holder
        )
        self.assertIsNone(exc)

    def test_a_soft_deleted_task_reads_as_a_missing_one_with_the_flag_on_and_as_deleted_off(self):
        """The task the audit named: `has been deleted` told a teammate the task had existed."""
        dead = self.make_task(self.live, "Dead", 5, is_deleted=True)
        _, on = self.run_tool("add_comment", {"task_id": dead.task_id, "body_text": "x"}, self.user)
        _, missing = self.run_tool("add_comment", {"task_id": MISSING, "body_text": "x"}, self.user)
        self.assertEqual(self.norm(on, dead.task_id), self.norm(missing, MISSING))
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            _, off = self.run_tool(
                "add_comment", {"task_id": dead.task_id, "body_text": "x"}, self.user
            )
        self.assertIn("has been deleted", str(off))

    def test_the_bulk_update_and_the_dependency_update_name_no_gone_task(self):
        _, exc = self.run_tool(
            "update_tasks_bulk",
            {
                "updates": [
                    {"task_id": self.gone_task.task_id, "status": "Closed", "rationale": "r"}
                ]
            },
            self.holder,
        )
        _, missing = self.run_tool(
            "update_tasks_bulk",
            {"updates": [{"task_id": MISSING, "status": "Closed", "rationale": "r"}]},
            self.holder,
        )
        self.assertEqual(self.norm(exc, self.gone_task.task_id), self.norm(missing, MISSING))
        gone_edge = self.run_tool(
            "update_task_dependencies",
            {
                "add": [
                    {
                        "blocker_task_id": self.gone_task.task_id,
                        "blocked_task_id": self.live_task.task_id,
                    }
                ]
            },
            self.holder,
        )[1]
        missing_edge = self.run_tool(
            "update_task_dependencies",
            {"add": [{"blocker_task_id": MISSING, "blocked_task_id": self.live_task.task_id}]},
            self.holder,
        )[1]
        self.assertEqual(
            self.norm(gone_edge, self.gone_task.task_id), self.norm(missing_edge, MISSING)
        )
        gone.refresh_from_db() if (gone := TaskMaster.objects.get(pk=self.gone_task.pk)) else None
        self.assertEqual(gone.status, "Open")

    def test_a_gone_task_is_not_a_parent(self):
        for name, args in (
            ("create_task", {"project_id": self.live.project_id, "title": "x"}),
            ("create_task_plan", {"project_id": self.live.project_id, "tasks": [{"title": "x"}]}),
        ):
            with self.subTest(name):
                gone = self.run_tool(
                    name, {**args, "parent_task_id": self.gone_task.task_id}, self.user
                )[1]
                missing = self.run_tool(name, {**args, "parent_task_id": MISSING}, self.user)[1]
                self.assertIsNotNone(missing)
                self.assertEqual(
                    self.norm(gone, self.gone_task.task_id), self.norm(missing, MISSING)
                )

    def test_a_gone_parent_is_not_a_refusal_for_someone_who_could_not_read_it(self):
        """A member of the live project only: before the parent is judged as missing, the ACL answer would say
        "Not authorized to file a task under task N", which says the task exists."""
        other = User.objects.create_user(username="other", email="other@x.com", password="p")
        TeamMembers.objects.create(team=self.team, attendee=other)
        ProjectMembers.objects.create(team=self.team, project=self.live, attendee=other)
        args = {"project_id": self.live.project_id, "title": "x"}
        _, gone = self.run_tool(
            "create_task", {**args, "parent_task_id": self.gone_task.task_id}, other
        )
        _, missing = self.run_tool("create_task", {**args, "parent_task_id": MISSING}, other)
        self.assertIsNotNone(missing)
        self.assertIs(type(gone), type(missing))
        self.assertEqual(self.norm(gone, self.gone_task.task_id), self.norm(missing, MISSING))
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            _, off = self.run_tool(
                "create_task", {**args, "parent_task_id": self.gone_task.task_id}, other
            )
        self.assertIn("Not authorized", str(off))

    def test_the_helper_answers_false_with_the_flag_off(self):
        task = TaskMaster.objects.select_related("project").get(pk=self.gone_task.pk)
        self.assertTrue(project_gone.task_is_gone(task))
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertFalse(project_gone.task_is_gone(task))
            self.assertEqual(str(project_gone.live_tasks_q()), "(AND: )")


class McpOnlyAndServiceTests(World):
    """The MCP-only tools and the services behind the briefing, the timeline and the report."""

    def mcp(self, person, name, args):
        self._key(user=person)
        text, is_error = self._tool_text(self._call(name, args))
        return text, is_error

    def test_the_timeline_and_the_item_line_answer_a_gone_task_as_a_missing_one(self):
        for who in ("owner", "member", "assignee not on the project", "teammate"):
            person = self.people[who][0]
            for name, build in (
                ("get_timeline", lambda t: {"anchor_type": "task", "anchor_id": str(t)}),
                ("get_item_line", lambda t: {"task_id": t}),
            ):
                with self.subTest(f"{name} as {who}"):
                    gone = self.mcp(person, name, build(self.gone_task.task_id))
                    missing = self.mcp(person, name, build(MISSING))
                    self.assertEqual(
                        (re.sub(r"\d+", "N", gone[0]), gone[1]),
                        (re.sub(r"\d+", "N", missing[0]), missing[1]),
                    )

    def test_a_display_id_of_a_gone_task_resolves_as_a_missing_display_id(self):
        for who in ("owner", "assignee not on the project"):
            person = self.people[who][0]
            gone = self.mcp(person, "get_task", {"task_id": "GON-1"})
            missing = self.mcp(person, "get_task", {"task_id": "ZZZ-9"})
            self.assertEqual(
                (gone[0].replace("GON-1", "X-1"), gone[1]),
                (missing[0].replace("ZZZ-9", "X-1"), missing[1]),
                who,
            )
            self.assertTrue(gone[1])
        live = self.mcp(self.user, "get_task", {"task_id": "LIV-1"})
        self.assertFalse(live[1])

    def test_the_progress_history_answers_a_gone_task_as_a_missing_one(self):
        gone = self.mcp(self.user, "get_progress_history", {"task_id": self.gone_task.task_id})
        missing = self.mcp(self.user, "get_progress_history", {"task_id": MISSING})
        self.assertEqual(
            (re.sub(r"\d+", "N", gone[0]), gone[1]), (re.sub(r"\d+", "N", missing[0]), missing[1])
        )

    def test_the_briefing_never_names_the_gone_task(self):
        for who in ("owner", "member", "assignee not on the project"):
            person = self.people[who][0]
            text, _ = self.mcp(person, "get_my_briefing", {})
            self.assertEqual(has_marker(text), [], who)

    def test_the_label_access_rule_and_its_sql_twin_agree_and_refuse_a_gone_task(self):
        for who, (person, _team) in self.people.items():
            access = LabelAccess(self.ctx(person))
            sql = TaskMaster.objects.filter(item_lines.readable_task_q(self.ctx(person), ""))
            for task in (self.gone_task, self.live_task):
                with self.subTest(f"{who} {task.title}"):
                    row = TaskMaster.objects.select_related("project").get(pk=task.pk)
                    self.assertEqual(access.task_readable(row), sql.filter(pk=task.pk).exists())
                    if task is self.gone_task:
                        self.assertFalse(access.task_readable(row))
                        self.assertFalse(sql.filter(pk=task.pk).exists())
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            access = LabelAccess(self.ctx(self.holder))
            row = TaskMaster.objects.select_related("project").get(pk=self.gone_task.pk)
            self.assertTrue(access.task_readable(row))
            self.assertTrue(
                TaskMaster.objects.filter(
                    item_lines.readable_task_q(self.ctx(self.holder), ""), pk=self.gone_task.pk
                ).exists()
            )

    def test_chips_links_and_report_context_leave_the_gone_task_out(self):
        from origin.search_engine.agent.tools.entity_chips import _milestone_chip, _task_chip
        from origin.search_engine.agent.tools.entity_links import resolve_note_entity_link
        from origin.services.report_ai import context

        team, uid = str(self.team.team_id), str(self.user.id)
        self.assertIsNone(_task_chip({"task_id": self.gone_task.task_id}, team, uid))
        self.assertIsNotNone(_task_chip({"task_id": self.live_task.task_id}, team, uid))
        self.assertIsNone(_milestone_chip(self.gone_ms.milestone_id, team, uid))
        self.assertIsNone(
            resolve_note_entity_link(
                f"task:{self.gone_task.task_id}", team_id=team, user_id=str(self.user.id)
            )
        )
        self.assertIsNone(
            resolve_note_entity_link(
                f"milestone:{self.gone_ms.milestone_id}", team_id=team, user_id=str(self.user.id)
            )
        )
        self.assertIsNotNone(
            resolve_note_entity_link(
                f"task:{self.live_task.task_id}", team_id=team, user_id=str(self.user.id)
            )
        )
        self.assertEqual(
            context.can_open(
                self.ctx(self.holder), [self.gone_task.task_id, self.live_task.task_id]
            ),
            {self.live_task.task_id},
        )
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIsNotNone(_task_chip({"task_id": self.gone_task.task_id}, team, uid))
            self.assertIsNotNone(
                resolve_note_entity_link(
                    f"task:{self.gone_task.task_id}", team_id=team, user_id=str(self.user.id)
                )
            )
            self.assertEqual(
                context.can_open(self.ctx(self.holder), [self.gone_task.task_id]),
                {self.gone_task.task_id},
            )

    def test_citations_and_the_report_links_leave_it_out(self):
        from origin.search_engine.agent import citation_resolver
        from origin.services.schedule_jobs.report_kit import Links

        built = citation_resolver._resolve_tasks(
            {self.gone_task.task_id, self.live_task.task_id},
            str(self.team.team_id),
            str(self.holder.id),
            lambda tid, title, pid: {"task_id": tid},
        )
        self.assertEqual([b["task_id"] for b in built], [self.live_task.task_id])
        milestones = citation_resolver._resolve_milestones(
            {self.gone_ms.milestone_id, self.live_ms.milestone_id},
            str(self.team.team_id),
            str(self.user.id),
            lambda mid, title, pid, tid: {"milestone_id": mid},
        )
        self.assertEqual([m["milestone_id"] for m in milestones], [self.live_ms.milestone_id])
        links = Links(self.ctx(self.user))
        links.preload(tasks=[self.gone_task.task_id, self.live_task.task_id])
        self.assertIsNone(links._facts[self.gone_task.task_id])
        self.assertIsNotNone(links._facts[self.live_task.task_id])

    def test_a_live_task_under_a_parent_of_a_gone_project_does_not_name_the_parent(self):
        child = self.make_task(
            self.live, "Child of a gone parent", 3, parent_task_id=self.gone_task.task_id
        )
        result, exc = self.run_tool("fetch_task", {"task_id": child.task_id}, self.user)
        self.assertIsNone(exc)
        self.assertEqual(has_marker(json.dumps(result, default=str)), [])
        self.assertIsNone(result.get("parent_display_id"))
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            result, _ = self.run_tool("fetch_task", {"task_id": child.task_id}, self.user)
        self.assertEqual(result.get("parent_display_id"), "GON-1")

    def test_the_milestone_that_was_converted_to_a_gone_task_does_not_name_it(self):
        from origin.search_engine.agent.tools import manage_milestone

        converted = MilestoneMaster.objects.create(
            team=self.team,
            project=self.live,
            title="Converted",
            reporter=self.user,
            is_deleted=True,
            converted_to_task=self.gone_task,
        )
        ctx = self.ctx(self.user)
        self.assertEqual(
            manage_milestone._already_gone(converted, ctx),
            f"Milestone {converted.milestone_id} has been deleted.",
        )
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIn("GON-1", manage_milestone._already_gone(converted, ctx))

    def test_the_timeline_refuses_a_gone_task_with_the_class_and_the_words_of_a_missing_one(self):
        from origin.services import timeline

        for ref in (self.gone_task.task_id, "GON-1"):
            gone = ZZ = None
            try:
                timeline._open_task(self.ctx(self.user), LabelAccess(self.ctx(self.user)), ref)
            except ToolError as exc:
                gone = exc
            try:
                timeline._open_task(
                    self.ctx(self.user),
                    LabelAccess(self.ctx(self.user)),
                    MISSING if isinstance(ref, int) else "ZZZ-9",
                )
            except ToolError as exc:
                ZZ = exc
            self.assertIsNotNone(gone, ref)
            self.assertIs(type(gone), type(ZZ), ref)
            self.assertEqual(str(gone), str(ZZ), ref)

    def test_the_milestone_chip_of_a_backing_task_in_a_gone_project_is_not_made(self):
        from origin.search_engine.agent.tools.entity_chips import _milestone_chip

        MilestoneMaster.objects.filter(pk=self.gone_ms.pk).update(task=self.gone_task)
        self.assertIsNone(
            _milestone_chip(self.gone_ms.milestone_id, str(self.team.team_id), str(self.user.id))
        )
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIsNotNone(
                _milestone_chip(
                    self.gone_ms.milestone_id, str(self.team.team_id), str(self.user.id)
                )
            )


class ThreadClaimedByAGoneTaskTests(SequenceScenario):
    """A thread a task of a soft-deleted project claims tells nothing of that task: its CREATED and STATUS rows are
    headlines with the task's id, which `get_timeline` gives even to a member who may not open the task."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def kinds(self, user, seq):
        self._mcp(user)
        text, is_error = self._tool_text(
            self._call(
                "get_timeline", {"anchor_type": "thread", "anchor_id": str(seq.thread_root.id)}
            )
        )
        self.assertFalse(is_error, text)
        result = json.loads(text[text.index("{") :])
        return [e["kind"] for e in result["events"]], text

    def test_the_thread_timeline_drops_the_task_rows_of_a_gone_project(self):
        seq = self.build_sequence(client_on_project=False)
        for user in (seq.a, seq.b):
            kinds, _ = self.kinds(user, seq)
            self.assertIn("created", kinds)  # live: the headline is there
        ProjectMaster.objects.filter(pk=seq.project.pk).update(is_deleted=True)
        for user in (seq.a, seq.b):
            kinds, text = self.kinds(user, seq)
            self.assertNotIn("created", kinds)
            self.assertNotIn("status", kinds)
            self.assertNotIn(seq.task.display_id, text)
            self.assertIn(
                "message", kinds
            )  # the chat's own messages are the chat's, not the task's
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            kinds, _ = self.kinds(seq.a, seq)
        self.assertIn("created", kinds)

    def test_a_member_who_may_open_it_loses_the_link_too(self):
        seq = self.build_sequence(client_on_project=True)
        ProjectMaster.objects.filter(pk=seq.project.pk).update(is_deleted=True)
        self._mcp(seq.b)
        text, _ = self._tool_text(
            self._call(
                "get_timeline", {"anchor_type": "thread", "anchor_id": str(seq.thread_root.id)}
            )
        )
        self.assertNotIn('"anchor_type": "task"', text)


class PaidEvalSeedTests(BaseAPITestCase):
    """The world of the paid case `t1590.deleted-project-task` (`seed_mention_hardening`, project EMD, a task
    EMD-1 whose reporter is the viewer), read as the viewer with every read tool the model plausibly reaches for.
    The case now fails when the answer or ANY tool result names the task id or the project, so this is the
    $0 check that no path left alone by this change hands the model one of them."""

    FORBIDDEN = ("EMD-1", "Eval wound-down project", "Kestrel", "4,200", "Renew the harbour lease")
    TOOLS = (
        ("list_tasks", {}),
        ("list_tasks", {"status": ["Open", "WIP", "Blocked", "Pending", "Closed"]}),
        ("get_my_focus_tasks", {}),
        ("get_stale_tasks", {}),
        ("get_my_task_summary", {}),
        ("get_team_task_summary", {}),
        ("list_projects", {}),
        ("list_my_chats", {}),
        ("list_my_unread", {}),
        ("list_my_mentions", {}),
        ("list_my_inbox", {}),
        ("get_current_user", {}),
        ("get_my_blockers", {}),
        ("get_my_schedule", {}),
        ("list_today_todos", {}),
        ("list_uncompleted_todos", {}),
    )

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = {
            "team": "v1",
            "person": "b",
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
        }
        seeds.apply(["mention_hardening"], self.info)
        self.ctx = ToolContext(team_id=self.info["team_id"], user_id=self.info["user_id"])
        self.gone = ProjectMaster.objects.get(team=self.team, code="EMD")
        self.task = TaskMaster.objects.get(project=self.gone)

    def test_the_seed_is_what_the_case_assumes(self):
        self.assertTrue(self.gone.is_deleted)
        self.assertEqual(self.task.display_id, "EMD-1")
        self.assertEqual(str(self.task.reporter_id), str(self.user.id))

    def test_no_read_tool_hands_the_model_the_task_or_the_project(self):
        for name, args in self.TOOLS:
            with self.subTest(f"{name} {args}"):
                try:
                    text = json.dumps(REGISTRY[name].run(dict(args), self.ctx), default=str)
                except ToolError as exc:
                    text = str(exc)
                self.assertEqual([w for w in self.FORBIDDEN if w in text], [], text[:400])

    def test_the_tools_that_take_the_id_say_it_is_not_found(self):
        for name in ("fetch_task", "get_task_history", "get_task_blockers"):
            with self.subTest(name):
                with self.assertRaises(ToolError) as raised:
                    REGISTRY[name].run({"task_id": self.task.task_id}, self.ctx)
                self.assertEqual(str(raised.exception), f"Task {self.task.task_id} not found.")

    def test_the_flag_off_arm_still_reaches_it_which_is_why_the_case_needs_the_arm(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            text = json.dumps(REGISTRY["list_tasks"].run({}, self.ctx), default=str)
        self.assertIn("EMD-1", text)

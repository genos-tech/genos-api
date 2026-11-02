"""Over MCP, the numbers about the caller and the team can be asked for (MIP-930).

Twelve reads the in-app assistant has always had were withheld here: "what is
blocking me?", "who is overloaded?", "what is stuck?", "how is project X going?"
could only be approximated by paging through list calls. The tool-level rules
are in the tools' own tests; this is what the endpoint adds: that they are
published and read-only, that a read key reaches them, that a call drives the
real tool, and that nothing outside the caller's projects comes back.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools

User = get_user_model()

NUMBERS = {
    "get_my_blockers",
    "list_my_inbox",
    "get_my_throughput",
    "get_workload_distribution",
    "get_top_task_closers",
    "get_task_throughput_stats",
    "get_stale_tasks",
    "get_team_task_summary",
    "get_project_activity_ranking",
    "get_milestone_assignee_counts",
    "get_project_summary",
    "get_my_task_summary",
}

# Registry tools that are not on the catalog, each for a reason. A new tool has to
# be published or added here with its reason, so the decision is never skipped.
WITHHELD = {
    # "Milestones I am on" is `list_milestones` with the caller's own `assignee_id`.
    "list_my_milestones": "list_milestones takes assignee_id",
    # Tasks over a due range, plus the calendar: `list_tasks` and `list_calendar_events`.
    "get_my_schedule": "composable from list_tasks and list_calendar_events",
    # A connected client has its own web search and its own memory.
    "search_web": "the client has its own",
    "search_past_conversations": "the client has its own",
}


class McpNumbersBase(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)

    def ask(self, name, arguments=None):
        result = self._result(self._call(name, arguments or {}))
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class TestThePublishedSet(McpNumbersBase):
    def test_all_twelve_are_published_and_read_only(self):
        listed = self.listed()
        self.assertLessEqual(NUMBERS, set(listed))
        for name in NUMBERS:
            tool = tools.BY_NAME[name]
            self.assertFalse(tool.write, name)
            self.assertFalse(tool.destructive, name)
            self.assertFalse(REGISTRY[tool.registry_name].requires_approval, name)

    def test_every_registry_tool_is_published_or_withheld_on_purpose(self):
        published = {t.registry_name for t in tools.CATALOG}
        self.assertEqual(
            set(REGISTRY) - published,
            set(WITHHELD),
            "A tool is on the registry but neither on the MCP catalog nor in "
            "WITHHELD. Publish it, or withhold it here with the reason.",
        )

    def test_a_withheld_tool_really_is_not_published(self):
        for name in WITHHELD:
            self.assertNotIn(name, {t.name for t in tools.CATALOG})
            self.assertNotIn(name, {t.registry_name for t in tools.CATALOG})

    def test_no_description_points_at_a_tool_this_surface_does_not_have(self):
        """`_rewrite_refs` renames a tool, it cannot remove a reference to an
        absent one, so the caller would be told to call something that 404s."""
        absent = set(REGISTRY) - {t.name for t in tools.CATALOG}
        for name, tool in self.listed().items():
            if name not in NUMBERS:
                continue
            quoted = set(re.findall(r"`([a-z_]+)`", str(tool)))
            self.assertFalse(quoted & absent, f"{name} mentions {quoted & absent}")

    def test_milestones_i_am_on_is_answered_by_list_milestones(self):
        description = self.listed()["list_milestones"]["description"]
        self.assertIn("get_current_user", description)
        self.assertIn("assignee_id", description)

    def test_a_read_only_key_can_reach_all_of_them(self):
        args = {
            "get_top_task_closers": {"days": 7},
            "get_task_throughput_stats": {"days": 7},
            "get_stale_tasks": {"stale_days": 1},
            "get_project_summary": {"project_id": self.project.project_id},
            "get_project_activity_ranking": {"metric": "task_count"},
        }
        for name in sorted(NUMBERS):
            with self.subTest(tool=name):
                self.ask(name, args.get(name))

    def test_a_result_still_leads_with_the_untrusted_notice(self):
        result = self._result(self._call("get_team_task_summary", {}))
        self.assertTrue(result["content"][0]["text"].startswith(tools.UNTRUSTED_NOTICE))

    def test_a_required_argument_cannot_be_left_out(self):
        for name in ("get_top_task_closers", "get_stale_tasks", "get_project_activity_ranking"):
            with self.subTest(tool=name):
                result = self._result(self._call(name, {}))
                self.assertTrue(result.get("isError"), result)


class TestTheCallsDriveTheRealTools(McpNumbersBase):
    def setUp(self):
        super().setUp()
        self.second = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user2,
            title="Write the release notes",
            status="WIP",
            project_task_number=8,
        )
        TaskMaster.objects.filter(pk=self.second.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=30)
        )

    def test_the_project_summary_counts_the_project(self):
        out = self.ask("get_project_summary", {"project_id": self.project.project_id})
        self.assertEqual(out["total_tasks"], 2)
        self.assertEqual(out["status_breakdown"]["Open"], 1)
        self.assertEqual(out["status_breakdown"]["WIP"], 1)

    def test_the_team_summary_counts_every_project_the_caller_is_in(self):
        out = self.ask("get_team_task_summary")
        self.assertEqual(out["total_tasks"], 2)

    def test_my_summary_counts_only_my_tasks(self):
        out = self.ask("get_my_task_summary")
        self.assertEqual(out["total"], 1)

    def test_stale_tasks_finds_the_one_nobody_touched(self):
        out = self.ask("get_stale_tasks", {"stale_days": 14})
        self.assertEqual([t["title"] for t in out["tasks"]], ["Write the release notes"])
        self.assertGreaterEqual(out["tasks"][0]["days_stale"], 30)

    def test_workload_has_a_row_per_assignee(self):
        out = self.ask("get_workload_distribution")
        by_user = {row["username"]: row for row in out["assignees"]}
        self.assertEqual(by_user[self.user.username]["open_count"], 1)
        self.assertEqual(by_user[self.user2.username]["wip_count"], 1)

    def test_the_ranking_puts_the_busiest_project_first(self):
        out = self.ask("get_project_activity_ranking", {"metric": "task_count", "limit": 5})
        self.assertEqual(out["projects"][0]["project_name"], "Website Redesign")
        self.assertEqual(out["projects"][0]["count"], 2)

    def test_blockers_are_empty_when_nothing_blocks_me(self):
        out = self.ask("get_my_blockers")
        self.assertEqual(out["blocked_on_me"], [])
        self.assertEqual(out["blocking_others"], [])

    def test_the_inbox_shows_a_note_access_request_with_its_target(self):
        InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_body="may I read this?",
            item_type=4,
            item_optionals={"note_type": 1, "note_id": 9, "note_title": "Plan"},
        )
        out = self.ask("list_my_inbox", {"category": "requests"})
        (item,) = out["items"]
        self.assertEqual(item["item_type_label"], "note_access_request")
        self.assertEqual(item["request_status"], "pending")
        self.assertEqual(item["target"], {"note_type": 1, "note_id": 9})


class TestNothingOutsideMyProjectsComesBack(McpNumbersBase):
    def setUp(self):
        super().setUp()
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw-outsider-123"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other@team.example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        self.other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Secret",
            code="SEC",
            owner=outsider,
            project_system_user=outsider,
        )
        ProjectMembers.objects.create(
            team=other_team, project=self.other_project, attendee=outsider
        )
        self.secret = TaskMaster.objects.create(
            team=other_team,
            project=self.other_project,
            reporter=outsider,
            assignee=outsider,
            title="Layoffs plan",
            status="Open",
            project_task_number=1,
        )
        TaskMaster.objects.filter(pk=self.secret.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=90)
        )

    def test_a_project_summary_for_a_project_i_am_not_in_is_refused(self):
        result = self._result(
            self._call("get_project_summary", {"project_id": self.other_project.project_id})
        )
        self.assertTrue(result.get("isError"), result)
        self.assertNotIn("Layoffs", str(result))

    def test_the_other_projects_tasks_are_not_in_any_roll_up(self):
        self.assertEqual(self.ask("get_team_task_summary")["total_tasks"], 1)
        for name, args in (
            ("get_stale_tasks", {"stale_days": 1}),
            ("get_workload_distribution", {}),
            ("get_project_activity_ranking", {"metric": "task_count"}),
        ):
            with self.subTest(tool=name):
                text = str(self.ask(name, args))
                self.assertNotIn("Layoffs", text)
                self.assertNotIn("Secret", text)
                self.assertNotIn("outsider", text)

    def test_narrowing_to_a_project_i_am_not_in_shows_nothing_of_it(self):
        for name, args in (
            ("get_stale_tasks", {"stale_days": 1, "project_id": self.other_project.project_id}),
            ("get_top_task_closers", {"days": 30, "project_id": self.other_project.project_id}),
            (
                "get_task_throughput_stats",
                {"days": 30, "project_id": self.other_project.project_id},
            ),
        ):
            with self.subTest(tool=name):
                result = self._result(self._call(name, args))
                self.assertNotIn("Layoffs", str(result))
                self.assertNotIn("outsider", str(result))

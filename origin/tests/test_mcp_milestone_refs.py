"""A milestone is named by its display id, and `list_projects` carries codes (MIP-852).

People say "MIP-617", which is a milestone's BACKING TASK. The milestone tools
took only the numeric milestone id, and nothing turned one into the other
without scanning `list_milestones`. Now every tool argument that takes a
milestone also takes that display id or the backing task's URL, resolved in
`mcp/resolve.py`; `list_projects` returns each project's short code and its
`name_filter` matches it; `list_milestones` returns the backing display id.
"""

from __future__ import annotations

from unittest import mock

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.agent.tools.base import ToolError
from origin.services.milestone_service import create_milestone
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools
from origin.views.public.mcp.resolve import (
    NO_SUCH_MILESTONE_ID,
    resolve_milestone_ref,
)


class MilestoneRefBase(McpBase):
    def setUp(self):
        super().setUp()
        self.milestone = create_milestone(
            self.project, reporter_id=str(self.user.id), title="Launch the redesign"
        )
        self.backing = self.milestone.task
        self.display = self.backing.display_id  # e.g. WRD-8
        self.team_id = str(self.team.team_id)

    def _text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def _other_team_milestone(self):
        other_team = TeamMaster.objects.create(team_name="Elsewhere", owner=self.user2)
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=self.user2)
        return create_milestone(other_project, reporter_id=str(self.user2.id), title="Secret")


class TestResolveMilestoneRef(MilestoneRefBase):
    def test_a_display_id_resolves_to_the_milestone_not_the_task(self):
        self.assertNotEqual(self.milestone.milestone_id, self.backing.task_id)
        self.assertEqual(
            resolve_milestone_ref(self.display, self.team_id), self.milestone.milestone_id
        )

    def test_a_task_url_resolves_to_the_milestone(self):
        url = (
            f"https://app.genosai.dev/workspace/tasks/project/{self.project.project_id}"
            f"/task/{self.backing.task_id}"
        )
        self.assertEqual(resolve_milestone_ref(url, self.team_id), self.milestone.milestone_id)

    def test_a_bare_number_stays_a_milestone_id_never_a_task_id(self):
        """Existing callers must not change meaning: `98` is milestone 98 even
        when task 98 is some other row's backing task."""
        for ref in (str(self.backing.task_id), f"#{self.backing.task_id}", self.backing.task_id):
            self.assertEqual(resolve_milestone_ref(ref, self.team_id), int(str(ref).lstrip("#")))
        self.assertEqual(
            resolve_milestone_ref(str(self.milestone.milestone_id), self.team_id),
            self.milestone.milestone_id,
        )

    def test_empty_string_passes_through_for_detach(self):
        self.assertEqual(resolve_milestone_ref("", self.team_id), "")
        self.assertEqual(resolve_milestone_ref("  ", self.team_id), "")

    def test_a_task_that_is_not_a_milestone_is_refused_clearly(self):
        with self.assertRaises(ToolError) as caught:
            resolve_milestone_ref("WRD-7", self.team_id)
        self.assertIn("not a milestone", str(caught.exception))

    def test_an_unknown_display_id_is_refused(self):
        with self.assertRaises(ToolError):
            resolve_milestone_ref("WRD-9999", self.team_id)

    def test_another_teams_milestone_does_not_resolve(self):
        other = self._other_team_milestone()
        with self.assertRaises(ToolError):
            resolve_milestone_ref(other.task.display_id, self.team_id)
        # A pasted URL names the task id without a team check of its own; the
        # milestone lookup is what keeps it team-scoped.
        with self.assertRaises(ToolError):
            resolve_milestone_ref(f"/workspace/tasks/project/1/task/{other.task_id}", self.team_id)

    def test_a_deleted_milestone_does_not_resolve(self):
        self.milestone.is_deleted = True
        self.milestone.save(update_fields=["is_deleted"])
        with self.assertRaises(ToolError):
            resolve_milestone_ref(self.display, self.team_id)

    def test_unreadable_text_is_left_for_the_tool_to_refuse(self):
        self.assertEqual(resolve_milestone_ref("the redesign", self.team_id), "the redesign")

    def test_quiet_mode_never_says_whether_something_exists(self):
        other = self._other_team_milestone()
        for ref in ("WRD-7", "WRD-9999", other.task.display_id):
            self.assertEqual(
                resolve_milestone_ref(ref, self.team_id, quiet=True), NO_SUCH_MILESTONE_ID
            )
        self.assertEqual(
            resolve_milestone_ref(self.display, self.team_id, quiet=True),
            self.milestone.milestone_id,
        )


class TestMilestoneToolsTakeADisplayId(MilestoneRefBase):
    def test_get_milestone_summary(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._text("get_milestone_summary", {"milestone_id": self.display})
        self.assertFalse(is_error, text)
        self.assertIn("Launch the redesign", text)

    def test_get_milestone_summary_with_a_non_milestone_says_so(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._text("get_milestone_summary", {"milestone_id": "WRD-7"})
        self.assertTrue(is_error)
        self.assertIn("not a milestone", text)

    def test_get_milestone_summary_still_takes_the_numeric_id(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._text(
            "get_milestone_summary", {"milestone_id": self.milestone.milestone_id}
        )
        self.assertFalse(is_error, text)
        self.assertIn("Launch the redesign", text)

    def test_list_tasks(self):
        self._key(scope=SCOPE_READ)
        child = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Child in the milestone",
            milestone=self.milestone,
            project_task_number=50,
        )
        text, is_error = self._text("list_tasks", {"milestone_id": self.display})
        self.assertFalse(is_error, text)
        self.assertIn(child.title, text)

    def test_list_task_dependencies(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._text("list_task_dependencies", {"milestone_id": self.display})
        self.assertFalse(is_error, text)

    def test_create_task(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "create_task",
            {
                "project_id": self.project.project_id,
                "title": "Filed by display id",
                "milestone_id": self.display,
            },
        )
        self.assertFalse(is_error, text)
        made = TaskMaster.objects.get(title="Filed by display id")
        self.assertEqual(made.milestone_id, self.milestone.milestone_id)

    def test_create_task_plan_existing_milestone(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "create_task_plan",
            {
                "project_id": self.project.project_id,
                "existing_milestone_id": self.display,
                "tasks": [{"title": "Planned under the milestone"}],
            },
        )
        self.assertFalse(is_error, text)
        made = TaskMaster.objects.get(title="Planned under the milestone")
        self.assertEqual(made.milestone_id, self.milestone.milestone_id)

    def test_update_task_moves_by_display_id_and_detaches_with_empty_string(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "update_task", {"task_id": "WRD-7", "milestone_id": self.display}
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.milestone_id, self.milestone.milestone_id)

        text, is_error = self._text("update_task", {"task_id": "WRD-7", "milestone_id": ""})
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.milestone_id)

    def test_update_tasks_bulk_moves_by_display_id_and_detaches_with_empty_string(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "update_tasks_bulk",
            {
                "updates": [
                    {"task_id": "WRD-7", "milestone_id": self.display, "rationale": "group it"}
                ]
            },
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.milestone_id, self.milestone.milestone_id)

        text, is_error = self._text(
            "update_tasks_bulk",
            {"updates": [{"task_id": "WRD-7", "milestone_id": "", "rationale": "ungroup it"}]},
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.milestone_id)

    def test_a_non_milestone_in_a_batch_row_fails_the_call(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._text(
            "update_tasks_bulk",
            {"updates": [{"task_id": "WRD-7", "milestone_id": "WRD-7", "rationale": "x"}]},
        )
        self.assertTrue(is_error)
        self.assertIn("not a milestone", text)

    def test_every_milestone_argument_is_published_as_a_string_reference(self):
        """All ten arguments (nine top-level + the bulk row key)."""
        published = []
        for tool in tools.CATALOG:
            schema = tools.input_schema(tool)
            for arg in tool.milestone_ref_args:
                prop = schema["properties"][arg]
                self.assertEqual(prop["type"], "string", f"{tool.name}.{arg}")
                self.assertIn("display id", prop["description"], f"{tool.name}.{arg}")
                published.append(f"{tool.name}.{arg}")
            for list_arg, item_key in tool.nested_milestone_ref_args:
                prop = schema["properties"][list_arg]["items"]["properties"][item_key]
                self.assertEqual(prop["type"], "string")
                self.assertIn("display id", prop["description"])
                published.append(f"{tool.name}.{list_arg}[].{item_key}")
        self.assertEqual(len(published), 10, published)

    def test_every_declared_milestone_ref_arg_is_a_real_property(self):
        from origin.search_engine.agent.tools import get_mcp_tool

        for tool in tools.CATALOG:
            props = get_mcp_tool(tool.registry_name).parameters_schema.get("properties") or {}
            for arg in tool.milestone_ref_args:
                self.assertIn(arg, props, f"{tool.name} resolves absent `{arg}`")
            for list_arg, item_key in tool.nested_milestone_ref_args:
                items = (props.get(list_arg) or {}).get("items") or {}
                self.assertIn(item_key, items.get("properties") or {})


class TestSearchStaysQuiet(MilestoneRefBase):
    def _search(self, ref):
        self._key(scope=SCOPE_READ)
        with mock.patch.object(search_kb, "search", return_value={"results": []}) as run:
            text, is_error = self._text(
                "search_workspace", {"query": "launch", "milestone_id": ref}
            )
        return text, is_error, run

    def test_a_display_id_scopes_the_search_to_that_milestone(self):
        _text, is_error, run = self._search(self.display)
        self.assertFalse(is_error)
        self.assertEqual(run.call_args.kwargs["milestone_ids"], [str(self.milestone.milestone_id)])

    def test_a_task_that_is_not_a_milestone_matches_nothing_and_does_not_error(self):
        _text, is_error, run = self._search("WRD-7")
        self.assertFalse(is_error)
        self.assertEqual(run.call_args.kwargs["milestone_ids"], [str(NO_SUCH_MILESTONE_ID)])

    def test_an_unknown_display_id_looks_the_same(self):
        _text, is_error, run = self._search("WRD-9999")
        self.assertFalse(is_error)
        self.assertEqual(run.call_args.kwargs["milestone_ids"], [str(NO_SUCH_MILESTONE_ID)])


class TestProjectCodesAndMilestoneDisplayIds(MilestoneRefBase):
    def _projects(self, arguments=None):
        self._key(scope=SCOPE_READ)
        res = self._call("list_projects", arguments or {})
        self.assertFalse(self._tool_text(res)[1])
        return self._result(res)["structuredContent"]["projects"]

    def test_list_projects_returns_the_code(self):
        rows = self._projects()
        self.assertEqual([r["code"] for r in rows], ["WRD"])

    def test_name_filter_matches_the_code(self):
        self.assertEqual([r["code"] for r in self._projects({"name_filter": "wrd"})], ["WRD"])
        self.assertEqual(self._projects({"name_filter": "Redesign"})[0]["code"], "WRD")
        self.assertEqual(self._projects({"name_filter": "ZZZ"}), [])

    def test_list_milestones_returns_the_backing_display_id(self):
        self._key(scope=SCOPE_READ)
        res = self._call("list_milestones", {"project_id": self.project.project_id})
        rows = self._result(res)["structuredContent"]["milestones"]
        self.assertEqual(rows[0]["display_id"], self.display)
        self.assertEqual(rows[0]["task_id"], self.backing.task_id)

    def test_a_listed_display_id_round_trips_into_get_milestone_summary(self):
        self._key(scope=SCOPE_READ)
        res = self._call("list_milestones", {"project_id": self.project.project_id})
        shown = self._result(res)["structuredContent"]["milestones"][0]["display_id"]
        text, is_error = self._text("get_milestone_summary", {"milestone_id": shown})
        self.assertFalse(is_error, text)

    def test_a_project_without_a_code_gives_no_milestone_display_id(self):
        """`display_id` falls back to `#<task_id>`, which the resolver would
        read as a MILESTONE id. Better to give none."""
        self.project.code = None
        self.project.save(update_fields=["code"])
        self._key(scope=SCOPE_READ)
        res = self._call("list_milestones", {"project_id": self.project.project_id})
        rows = self._result(res)["structuredContent"]["milestones"]
        self.assertIsNone(rows[0]["display_id"])

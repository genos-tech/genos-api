"""A project's status names in the agent and MCP tools (MIP-2494, flag `AGENT_CUSTOM_STATUS`)."""

from django.conf import settings
from django.test import override_settings

from origin.models.project.prj_models import ProjectStatus
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tool_flag_params import with_flagged_params
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.milestone_service import create_milestone
from origin.services.project_statuses import ensure_project_statuses
from origin.tests.test_project_statuses import StatusTestBase

WRITES = ("create_task", "update_task", "manage_milestone")


def flag(value):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_CUSTOM_STATUS": value})


class ToolBase(StatusTestBase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def run_tool(self, tool, **args):
        return REGISTRY[tool].run(args, self.ctx)

    def name_of(self, task):
        task = TaskMaster.objects.select_related("custom_status").get(pk=task.pk)
        return task.status, task.custom_status.name if task.custom_status else None


class FlagOffTests(ToolBase):
    def test_nothing_is_declared(self):
        with flag(False):
            for name in (*WRITES, "create_task_plan", "update_tasks_bulk"):
                tool = REGISTRY[name]
                schema = with_flagged_params(name, tool.parameters_schema)
                self.assertNotIn("custom_status", str(schema), name)

    def test_the_argument_is_ignored_and_reads_say_nothing(self):
        with flag(False):
            task = self.task("Open", custom=self.builtin("In Review"))
            self.run_tool("update_task", task_id=task.task_id, custom_status="WIP", priority="Low")
            self.assertEqual(self.name_of(task), ("Open", "In Review"))
            self.assertNotIn("custom_status", self.run_tool("fetch_task", task_id=task.task_id))
            tags = self.run_tool("list_project_tags", project_id=self.project.project_id)
            self.assertNotIn("statuses", tags)


@flag(True)
class WriteTests(ToolBase):
    def test_the_writes_declare_it(self):
        for name in WRITES:
            schema = with_flagged_params(name, REGISTRY[name].parameters_schema)
            self.assertIn("custom_status", schema["properties"], name)
        for name, array in (("create_task_plan", "tasks"), ("update_tasks_bulk", "updates")):
            schema = with_flagged_params(name, REGISTRY[name].parameters_schema)
            self.assertIn("custom_status", schema["properties"][array]["items"]["properties"])

    def test_a_name_sets_the_status_of_its_category(self):
        self.add("Doing", "WIP")
        task = self.task("Open")
        result = self.run_tool(
            "update_task", task_id=task.task_id, custom_status=" doing ", reason="started"
        )
        self.assertEqual(self.name_of(task), ("WIP", "Doing"))
        self.assertEqual(result["custom_status"], "Doing")
        self.assertIn("custom_status", result["changed_fields"])
        self.assertNotIn("suggestions", result)

    def test_in_review_works_before_anyone_set_the_project_up(self):
        task = self.task("Open")
        self.run_tool("update_task", task_id=task.task_id, custom_status="In Review")
        self.assertEqual(self.name_of(task), ("Open", "In Review"))

    def test_a_plain_category_name_writes_no_rows(self):
        task = self.task("Open")
        self.run_tool("update_task", task_id=task.task_id, custom_status="WIP", reason="go")
        self.assertEqual(self.name_of(task), ("WIP", None))
        self.assertFalse(ProjectStatus.objects.filter(project=self.project).exists())

    def test_a_name_that_does_not_fit_is_suggested_and_the_rest_applies(self):
        ensure_project_statuses(self.project)
        off = self.add("Old", "WIP")
        ProjectStatus.objects.filter(pk=off.pk).update(archived_at="2026-01-01T00:00:00Z")
        for args in (
            {"custom_status": "Nope"},
            {"custom_status": "Old"},
            {"custom_status": "In Review", "status": "Closed"},
        ):
            task = self.task("Open")
            result = self.run_tool(
                "update_task",
                task_id=task.task_id,
                priority="High",
                reason="r",
                **args,
            )
            status = args.get("status", "Open")
            self.assertEqual(self.name_of(task), (status, None), args)
            (entry,) = [s for s in result["suggestions"] if s["kind"] == "custom_status"]
            self.assertIn({"name": "In Review", "category": "Open"}, entry["candidates"])
            self.assertNotIn("Old", [c["name"] for c in entry["candidates"]])

    def test_only_a_bad_name_changes_nothing_and_says_why(self):
        task = self.task("Open")
        result = self.run_tool("update_task", task_id=task.task_id, custom_status="Nope")
        self.assertEqual(result["changed_fields"], [])
        self.assertEqual(result["suggestions"][0]["kind"], "custom_status")

    def test_an_empty_name_removes_it(self):
        task = self.task("Open", custom=self.builtin("In Review"))
        self.run_tool("update_task", task_id=task.task_id, custom_status="")
        self.assertEqual(self.name_of(task), ("Open", None))

    def test_create_task(self):
        result = self.run_tool(
            "create_task",
            project_id=self.project.project_id,
            title="Review me",
            custom_status="In Review",
        )
        self.assertEqual((result["status"], result["custom_status"]), ("Open", "In Review"))
        wrong = self.run_tool(
            "create_task",
            project_id=self.project.project_id,
            title="Still made",
            custom_status="Nope",
        )
        self.assertEqual(wrong["status"], "Open")
        self.assertIn("custom_status", [s["kind"] for s in wrong["suggestions"]])

    def test_create_task_plan(self):
        self.add("Doing", "WIP")
        result = self.run_tool(
            "create_task_plan",
            project_id=self.project.project_id,
            tasks=[
                {"title": "a", "custom_status": "Doing"},
                {"title": "b", "custom_status": "Nope"},
            ],
        )
        first, second = result["tasks"]
        self.assertEqual(first["custom_status"], "Doing")
        self.assertEqual(TaskMaster.objects.get(pk=first["task_id"]).status, "WIP")
        self.assertNotIn("custom_status", second)
        self.assertIn(1, [s.get("index") for s in result["suggestions"]])

    def test_update_tasks_bulk(self):
        self.add("Doing", "WIP")
        a, b = self.task("Open"), self.task("Open")
        result = self.run_tool(
            "update_tasks_bulk",
            updates=[
                {"task_id": a.task_id, "custom_status": "Doing", "rationale": "started"},
                {"task_id": b.task_id, "custom_status": "Nope", "rationale": "started"},
            ],
        )
        self.assertEqual(self.name_of(a), ("WIP", "Doing"))
        self.assertEqual(self.name_of(b), ("Open", None))
        self.assertEqual([s["task_id"] for s in result["suggestions"]], [b.task_id])
        self.assertEqual(result["updated"][0]["custom_status"], "Doing")

    def test_manage_milestone(self):
        self.add("Doing", "WIP")
        made = self.run_tool(
            "manage_milestone",
            action="create",
            project_id=self.project.project_id,
            title="M",
            custom_status="Doing",
        )
        self.assertEqual((made["status"], made["custom_status"]), ("WIP", "Doing"))
        milestone = MilestoneMaster.objects.get(pk=made["milestone_id"])
        self.assertEqual(TaskMaster.objects.get(pk=milestone.task_id).custom_status.name, "Doing")
        changed = self.run_tool(
            "manage_milestone",
            action="update",
            milestone_id=milestone.milestone_id,
            custom_status="In Review",
            reason="back to review",
        )
        self.assertEqual((changed["status"], changed["custom_status"]), ("Open", "In Review"))


@flag(True)
class ReadTests(ToolBase):
    def test_reads_carry_the_name_only_when_there_is_one(self):
        named = self.task("Open", custom=self.builtin("In Review"))
        plain = self.task("Open")
        self.assertEqual(
            self.run_tool("fetch_task", task_id=named.task_id)["custom_status"], "In Review"
        )
        self.assertNotIn("custom_status", self.run_tool("fetch_task", task_id=plain.task_id))
        rows = self.run_tool("list_tasks", project_id=self.project.project_id)["tasks"]
        names = {r["task_id"]: r.get("custom_status") for r in rows}
        self.assertEqual((names[named.task_id], names[plain.task_id]), ("In Review", None))

    def test_milestone_reads_carry_it(self):
        row = self.builtin("In Review")
        m = create_milestone(self.project, reporter_id=self.user.id, title="M", custom_status=row)
        listed = self.run_tool("list_milestones", project_id=self.project.project_id)
        self.assertEqual(listed["milestones"][0]["custom_status"], "In Review")
        summary = self.run_tool("get_milestone_summary", milestone_id=m.milestone_id)
        self.assertEqual(summary["custom_status"], "In Review")

    def test_list_project_tags_lists_the_names_with_their_category(self):
        def statuses():
            return self.run_tool("list_project_tags", project_id=self.project.project_id)[
                "statuses"
            ]

        before = statuses()
        self.assertIn({"name": "In Review", "category": "Open"}, before)
        self.assertIn({"name": "Rejected", "category": "Rejected"}, before)
        self.assertFalse(ProjectStatus.objects.filter(project=self.project).exists())
        self.add("Doing", "WIP")
        self.assertIn({"name": "Doing", "category": "WIP"}, statuses())

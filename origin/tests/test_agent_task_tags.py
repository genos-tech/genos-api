"""Agent / MCP task tags (MIP-707).

Contract under test:

  * `list_project_tags` returns ONE project's catalogue, behind the same
    tenant + membership guard as the REST tag list.
  * `fetch_task` / `list_tasks` return a task's tag names.
  * `create_task`, `create_task_plan` and `update_task` take tag NAMES,
    matched against the task's OWN project's `ProjectTags`, and store the
    catalogue's `{tagName, tagColor, tagTextColor}` — the shape a tag
    picked in the UI has.
  * An unknown name — including one that exists only on another project
    — is refused and nothing is written; tags are never created.
  * `update_task` replaces the set, `[]` clears, and a change records a
    `tags_changed` history row, as a UI edit does.
"""

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.list_project_tags import LIST_PROJECT_TAGS
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase
from .test_mcp_endpoint import McpBase

BUG = {"tagName": "bug", "tagColor": "#ff0000", "tagTextColor": "#ffffff"}
FRONTEND = {"tagName": "Frontend", "tagColor": "#00ff00", "tagTextColor": "#000000"}


class TaskTagTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project("Website Redesign", "WRD")
        self._tag(self.project, 1, BUG)
        self._tag(self.project, 2, FRONTEND)
        # Same team, different project, its own vocabulary.
        self.other = self._project("Other", "OTH")
        self._tag(
            self.other, 1, {"tagName": "infra", "tagColor": "#0000ff", "tagTextColor": "#fff"}
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _project(self, name, code):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _tag(self, project, tag_id, tag):
        ProjectTags.objects.create(
            team=self.team,
            project=project,
            tag_id=tag_id,
            tag_name=tag["tagName"],
            tag_color=tag["tagColor"],
            tag_text_color=tag["tagTextColor"],
        )

    def _create(self, **args):
        out = CREATE_TASK.run(
            {"title": "T", "project_id": self.project.project_id, **args}, self.ctx
        )
        return TaskMaster.objects.get(task_id=out["task_id"]), out

    def _tag_rows(self, task):
        return list(TaskActivity.objects.filter(task=task, action_type=TaskActivityActionType.TAGS))

    def _task_count(self):
        return TaskMaster.objects.filter(is_milestone=False).count()


class ListProjectTagsTests(TaskTagTestBase):
    def test_returns_this_projects_catalogue_only(self):
        out = LIST_PROJECT_TAGS.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual(
            sorted((t["name"], t["color"], t["text_color"]) for t in out["tags"]),
            [("Frontend", "#00ff00", "#000000"), ("bug", "#ff0000", "#ffffff")],
        )

    def test_is_registered_and_categorised(self):
        self.assertIs(REGISTRY["list_project_tags"], LIST_PROJECT_TAGS)

    def test_non_member_is_refused(self):
        stranger_ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesRegex(ToolError, "not a member"):
            LIST_PROJECT_TAGS.run({"project_id": self.project.project_id}, stranger_ctx)

    def test_other_team_is_refused(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ProjectMaster.objects.filter(pk=self.project.pk).update(team=other_team)
        with self.assertRaisesRegex(ToolError, "different team"):
            LIST_PROJECT_TAGS.run({"project_id": self.project.project_id}, self.ctx)


class ReadTagsTests(TaskTagTestBase):
    def test_fetch_task_and_list_tasks_return_tag_names(self):
        task, _ = self._create(tags=["bug", "Frontend"])
        fetched = FETCH_TASK.run({"task_id": task.task_id}, self.ctx)
        self.assertEqual(fetched["tags"], ["bug", "Frontend"])
        listed = LIST_TASKS.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual([t["tags"] for t in listed["tasks"]], [["bug", "Frontend"]])

    def test_untagged_task_reads_as_empty_list(self):
        task, _ = self._create()
        self.assertEqual(FETCH_TASK.run({"task_id": task.task_id}, self.ctx)["tags"], [])


class CreateTaskTagsTests(TaskTagTestBase):
    def test_names_store_the_catalogue_shape_with_colours(self):
        task, out = self._create(tags=["bug", "Frontend"])
        self.assertEqual(task.tags, [BUG, FRONTEND])
        self.assertEqual(out["tags"], ["bug", "Frontend"])

    def test_case_insensitive_match_stores_the_catalogue_spelling(self):
        task, _ = self._create(tags=["frontend", "BUG"])
        self.assertEqual(task.tags, [FRONTEND, BUG])

    def test_duplicates_collapse(self):
        task, _ = self._create(tags=["bug", "bug", "BUG"])
        self.assertEqual(task.tags, [BUG])

    def test_ambiguous_case_insensitive_match_is_refused(self):
        self._tag(self.project, 3, {"tagName": "Bug", "tagColor": "#111", "tagTextColor": "#fff"})
        # Exact spellings still resolve…
        task, _ = self._create(tags=["Bug", "bug"])
        self.assertEqual([t["tagName"] for t in task.tags], ["Bug", "bug"])
        # …but a third spelling could mean either, so it is refused.
        with self.assertRaisesRegex(ToolError, "unknown tag"):
            self._create(tags=["BUG"])

    def test_unknown_name_is_refused_and_nothing_is_created(self):
        with self.assertRaisesRegex(ToolError, r"unknown tag\(s\) 'urgent'.*'bug'"):
            self._create(tags=["bug", "urgent"])
        self.assertEqual(self._task_count(), 0)
        self.assertFalse(ProjectTags.objects.filter(tag_name="urgent").exists())

    def test_another_projects_tag_is_refused(self):
        with self.assertRaisesRegex(ToolError, "unknown tag"):
            self._create(tags=["infra"])
        self.assertEqual(self._task_count(), 0)

    def test_omitted_or_empty_tags_leave_task_untagged(self):
        for args in ({}, {"tags": None}, {"tags": []}):
            task, out = self._create(**args)
            self.assertFalse(task.tags, args)
            self.assertEqual(out["tags"], [], args)

    def test_a_non_list_is_refused(self):
        with self.assertRaisesRegex(ToolError, "must be a list"):
            self._create(tags="bug")


class UpdateTaskTagsTests(TaskTagTestBase):
    def test_replaces_the_set_and_records_history(self):
        task, _ = self._create(tags=["bug"])
        out = UPDATE_TASK.run({"task_id": task.task_id, "tags": ["Frontend"]}, self.ctx)
        task.refresh_from_db()
        self.assertEqual(task.tags, [FRONTEND])
        self.assertEqual(out["changed_fields"], ["tags"])
        row = self._tag_rows(task)[-1]
        self.assertEqual(row.old_value, [BUG])
        self.assertEqual(row.new_value, [FRONTEND])

    def test_empty_list_clears(self):
        task, _ = self._create(tags=["bug"])
        out = UPDATE_TASK.run({"task_id": task.task_id, "tags": []}, self.ctx)
        task.refresh_from_db()
        self.assertFalse(task.tags)
        self.assertEqual(out["changed_fields"], ["tags(cleared)"])
        self.assertIsNone(self._tag_rows(task)[-1].new_value)

    def test_same_set_in_another_order_is_a_noop(self):
        task, _ = self._create(tags=["bug", "Frontend"])
        before = len(self._tag_rows(task))
        out = UPDATE_TASK.run({"task_id": task.task_id, "tags": ["Frontend", "bug"]}, self.ctx)
        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(len(self._tag_rows(task)), before)

    def test_omitted_tags_are_left_alone(self):
        task, _ = self._create(tags=["bug"])
        UPDATE_TASK.run({"task_id": task.task_id, "title": "renamed"}, self.ctx)
        task.refresh_from_db()
        self.assertEqual(task.tags, [BUG])

    def test_unknown_name_is_refused_and_nothing_changes(self):
        task, _ = self._create(tags=["bug"])
        with self.assertRaisesRegex(ToolError, "unknown tag"):
            UPDATE_TASK.run(
                {"task_id": task.task_id, "title": "renamed", "tags": ["infra"]}, self.ctx
            )
        task.refresh_from_db()
        self.assertEqual((task.title, task.tags), ("T", [BUG]))

    def test_matches_against_the_tasks_own_project(self):
        other_task = TaskMaster.objects.create(
            team=self.team, project=self.other, title="o", status="Open"
        )
        UPDATE_TASK.run({"task_id": other_task.task_id, "tags": ["infra"]}, self.ctx)
        other_task.refresh_from_db()
        self.assertEqual([t["tagName"] for t in other_task.tags], ["infra"])
        with self.assertRaisesRegex(ToolError, "unknown tag"):
            UPDATE_TASK.run({"task_id": other_task.task_id, "tags": ["bug"]}, self.ctx)

    def test_milestone_tags_land_on_the_milestone_too(self):
        """A milestone's authoritative values live on MilestoneMaster;
        writing only the backing row would be reverted by the next sync."""
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        backing = ensure_backing_task(milestone)
        UPDATE_TASK.run({"task_id": backing.task_id, "tags": ["bug"]}, self.ctx)
        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.tags, [BUG])
        self.assertEqual(backing.tags, [BUG])


class CreateTaskPlanTagsTests(TaskTagTestBase):
    def test_each_task_gets_its_own_tags(self):
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "tasks": [{"title": "A", "tags": ["bug"]}, {"title": "B"}],
            },
            self.ctx,
        )
        a, b = (TaskMaster.objects.get(task_id=t["task_id"]) for t in out["tasks"])
        self.assertEqual(a.tags, [BUG])
        self.assertFalse(b.tags)
        self.assertEqual([t["tags"] for t in out["tasks"]], [["bug"], []])

    def test_unknown_name_fails_the_whole_plan(self):
        with self.assertRaisesRegex(ToolError, r"tasks\[1\]\.tags: unknown tag"):
            CREATE_TASK_PLAN.run(
                {
                    "project_id": self.project.project_id,
                    "tasks": [{"title": "A", "tags": ["bug"]}, {"title": "B", "tags": ["infra"]}],
                },
                self.ctx,
            )
        self.assertEqual(self._task_count(), 0)


class McpTagFlowTests(McpBase):
    """The whole loop on the MCP surface: read the catalogue, tag a task
    by display id, read it back — and the history row names the key's
    user, as any API write does."""

    def test_list_tags_then_tag_a_task_then_read_it(self):
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name=BUG["tagName"],
            tag_color=BUG["tagColor"],
            tag_text_color=BUG["tagTextColor"],
        )
        self._key()
        catalogue = self._result(
            self._call("list_project_tags", {"project_id": self.project.project_id})
        )["structuredContent"]
        self.assertEqual([t["name"] for t in catalogue["tags"]], ["bug"])

        text, is_error = self._tool_text(
            self._call("update_task", {"task_id": "WRD-7", "tags": ["bug"]})
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.tags, [BUG])

        row = TaskActivity.objects.get(task=self.task, action_type=TaskActivityActionType.TAGS)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.metadata["via"]["kind"], "api_key")

        read = self._result(self._call("get_task", {"task_id": "WRD-7"}))["structuredContent"]
        self.assertEqual(read["tags"], ["bug"])

    def test_unknown_tag_is_a_tool_error_listing_the_choices(self):
        self._key()
        text, is_error = self._tool_text(
            self._call("update_task", {"task_id": "WRD-7", "tags": ["urgent"]})
        )
        self.assertTrue(is_error)
        self.assertIn("unknown tag", text)
        self.assertIsNone(self.task.tags)

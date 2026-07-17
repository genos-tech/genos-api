"""Tests for the monthly task/note creation caps.

Enforcement sites under test (all pre-flight check → 429/ToolError,
increment AFTER a successful create):

  REST: `POST /api/v2/task/` (TaskMasterView), personal + task note
  creates (`PersonalNoteMasterView` / `TaskNoteMasterView`; the chat
  note view uses the identical `check_monthly_creation_quota` guard).

  Agent tools: `create_task`, `create_note`, and `create_task_plan` —
  the last is LOOP-aware (a plan minting N tasks + optionally a new
  milestone's backing task must fit the remaining quota as a batch).

Quota numbers come from `TEST_QUOTAS` (free tier: 10 tasks / 5 notes
per month). The shipped defaults are `None` (dark) — covered by
`DefaultsAreDarkTests`.
"""

from django.test import override_settings
from django.utils import timezone

from origin.models.common.usage_models import ModelUsageCounter
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine import quota
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas


class CreationCapTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Cap Test Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()

    def seed_usage(self, key: str, count: int):
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=key,
            usage_date=timezone.now().date(),
            count=count,
        )

    def task_payload(self, **overrides):
        defaults = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "Cap Test Task",
            "priority": "Normal",
            "effort_level": "Moderate",
            "status": "Open",
            "content": None,
            "due_date": "2026-12-31",
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        defaults.update(overrides)
        return defaults

    def assert_limit_429(self, res, category: str):
        self.assertEqual(res.status_code, 429)
        self.assertTrue(res.data["limit_reached"])
        self.assertEqual(res.data["category"], category)
        self.assertIn("Upgrade your plan", res.data["error"])
        self.assertIn("used", res.data)
        self.assertIn("limit", res.data)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class TaskRestCapTests(CreationCapTestBase):
    def test_429_at_cap_and_no_row_created(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 10)
        before = TaskMaster.objects.count()
        res = self.client.post("/api/v2/task/", self.task_payload(), format="json")
        self.assert_limit_429(res, "task_create")
        self.assertEqual((res.data["used"], res.data["limit"]), (10, 10))
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_create_under_cap_charges_one_unit(self):
        res = self.client.post("/api/v2/task/", self.task_payload(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 1)

    def test_team_plan_lifts_the_cap(self):
        # free personal (10/mo) but the team pays for pro (100/mo).
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        quota.invalidate_effective_tier([self.user.id])
        self.seed_usage(quota.TASK_CREATE_KEY, 10)
        res = self.client.post("/api/v2/task/", self.task_payload(), format="json")
        self.assertEqual(res.status_code, 201)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class NoteRestCapTests(CreationCapTestBase):
    def _personal_payload(self):
        return {
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "title": "Cap Test Note",
            "body": [{"type": "paragraph", "content": []}],
        }

    def test_personal_note_429_at_cap(self):
        self.seed_usage(quota.NOTE_CREATE_KEY, 5)
        res = self.client.post("/api/v2/note/personal/", self._personal_payload(), format="json")
        self.assert_limit_429(res, "note_create")

    def test_personal_note_under_cap_charges(self):
        res = self.client.post("/api/v2/note/personal/", self._personal_payload(), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(quota.get_used_month(self.user.id, quota.NOTE_CREATE_KEY), 1)

    def test_task_note_429_at_cap(self):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="host task",
            status="Open",
        )
        self.seed_usage(quota.NOTE_CREATE_KEY, 5)
        res = self.client.post(
            "/api/v2/note/task/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "project_id": self.project.project_id,
                "task_id": task.task_id,
                "title": "Cap Test Task Note",
                "body": [{"type": "paragraph", "content": []}],
            },
            format="json",
        )
        self.assert_limit_429(res, "note_create")


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class AgentToolCapTests(CreationCapTestBase):
    def test_create_task_tool_blocked_at_cap(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 10)
        with self.assertRaises(ToolError) as caught:
            CREATE_TASK.run({"title": "T", "project_id": self.project.project_id}, self.ctx)
        self.assertIn("tasks for this month", str(caught.exception))
        self.assertEqual(TaskMaster.objects.count(), 0)

    def test_create_task_tool_charges_after_create(self):
        CREATE_TASK.run({"title": "T", "project_id": self.project.project_id}, self.ctx)
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 1)

    def test_create_note_tool_blocked_at_cap(self):
        self.seed_usage(quota.NOTE_CREATE_KEY, 5)
        with self.assertRaises(ToolError) as caught:
            CREATE_NOTE.run({"note_type": "personal", "title": "N"}, self.ctx)
        self.assertIn("notes for this month", str(caught.exception))

    def test_create_note_tool_charges_after_create(self):
        CREATE_NOTE.run({"note_type": "personal", "title": "N"}, self.ctx)
        self.assertEqual(quota.get_used_month(self.user.id, quota.NOTE_CREATE_KEY), 1)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class CreateTaskPlanCapTests(CreationCapTestBase):
    def _plan_args(self, n_tasks: int, with_milestone: bool = False):
        args = {
            "project_id": self.project.project_id,
            "tasks": [{"title": f"Task {i}"} for i in range(n_tasks)],
        }
        if with_milestone:
            args["milestone"] = {"title": "Cap Milestone"}
        return args

    def test_batch_over_remaining_rejected_before_any_row(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 8)  # 2 of 10 remain
        with self.assertRaises(ToolError) as caught:
            CREATE_TASK_PLAN.run(self._plan_args(3), self.ctx)
        msg = str(caught.exception)
        self.assertIn("would create 3 tasks", msg)
        self.assertIn("only 2", msg)
        self.assertEqual(TaskMaster.objects.count(), 0)
        self.assertEqual(MilestoneMaster.objects.count(), 0)

    def test_new_milestone_backing_task_counts_in_batch(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 8)  # 2 remain; milestone + 2 tasks = 3
        with self.assertRaises(ToolError):
            CREATE_TASK_PLAN.run(self._plan_args(2, with_milestone=True), self.ctx)
        self.assertEqual(TaskMaster.objects.count(), 0)

    def test_batch_within_remaining_succeeds_and_charges_batch(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 8)
        CREATE_TASK_PLAN.run(self._plan_args(2), self.ctx)
        self.assertEqual(TaskMaster.objects.filter(is_deleted=False).count(), 2)
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 10)

    def test_milestone_plan_charges_backing_task(self):
        CREATE_TASK_PLAN.run(self._plan_args(2, with_milestone=True), self.ctx)
        # 2 plan tasks + 1 milestone backing task.
        self.assertEqual(quota.get_used_month(self.user.id, quota.TASK_CREATE_KEY), 3)


class DefaultsAreDarkTests(CreationCapTestBase):
    """With the SHIPPED config (all new limits None) nothing is capped."""

    def test_task_create_unlimited_by_default(self):
        self.seed_usage(quota.TASK_CREATE_KEY, 10_000)
        res = self.client.post("/api/v2/task/", self.task_payload(), format="json")
        self.assertEqual(res.status_code, 201)

    def test_note_create_unlimited_by_default(self):
        self.seed_usage(quota.NOTE_CREATE_KEY, 10_000)
        res = self.client.post(
            "/api/v2/note/personal/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "title": "N",
                "body": [{"type": "paragraph", "content": []}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

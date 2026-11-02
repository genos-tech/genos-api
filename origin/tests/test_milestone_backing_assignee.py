"""A milestone's backing task shows the milestone's assignee (MIP-916).

The task table renders the milestone through its backing task, and
`sync_backing_task` mirrors the milestone's assignee onto it: the first of the
multi-assignee join, else the reporter. But the join is written AFTER that
mirror in `update_milestone` (and `create_milestone`), so after a POST or PATCH
with `assignee_ids` the backing task still showed the reporter. Only the
per-person endpoint synced afterwards. The fix is in the two shared functions, so
the app and the agent tools (`manage_milestone`, `create_task_plan`) get it together.

Also pinned: `create_task_plan` passes the reporter as a string id, and left
spurious `assignee_changed` / `reporter_changed` rows on the new milestone's
backing task.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
HISTORY = (TaskActivityActionType.ASSIGNEE, TaskActivityActionType.REPORTER)


class BackingAssigneeBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Backing",
            code="BAC",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.third = User.objects.create_user(
            username="third", email="third@example.com", password="thirdpass123"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.third)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.authenticate()

    def backing_assignee(self, milestone_id):
        return MilestoneMaster.objects.get(milestone_id=milestone_id).task.assignee_id

    def history_rows(self, task):
        return list(TaskActivity.objects.filter(task=task, action_type__in=HISTORY))


class RestEndpointTests(BackingAssigneeBase):
    def create(self, **extra):
        return self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "M1", **extra},
            format="json",
        )

    def patch(self, milestone_id, **body):
        return self.client.patch(
            reverse("milestone_detail", args=[milestone_id]), body, format="json"
        )

    def test_a_create_with_assignees_shows_the_first_on_the_backing_task(self):
        res = self.create(assignee_ids=[self.user2.id])
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.backing_assignee(res.data["milestone"]["milestoneId"]), self.user2.id)

    def test_a_patch_with_assignees_updates_the_backing_task(self):
        milestone_id = self.create().data["milestone"]["milestoneId"]
        self.assertEqual(self.backing_assignee(milestone_id), self.user.id)  # the reporter

        self.assertEqual(self.patch(milestone_id, assignee_ids=[self.third.id]).status_code, 200)
        self.assertEqual(self.backing_assignee(milestone_id), self.third.id)

    def test_clearing_the_assignees_falls_back_to_the_reporter(self):
        milestone_id = self.create(assignee_ids=[self.user2.id]).data["milestone"]["milestoneId"]
        self.assertEqual(self.patch(milestone_id, assignee_ids=[]).status_code, 200)
        self.assertEqual(self.backing_assignee(milestone_id), self.user.id)

    def test_it_matches_what_the_per_person_endpoint_does(self):
        via_post = self.create(assignee_ids=[self.third.id]).data["milestone"]["milestoneId"]
        via_endpoint = self.create().data["milestone"]["milestoneId"]
        self.client.post(
            reverse("milestone_assignees_add", args=[via_endpoint]),
            {"user_id": str(self.third.id)},
            format="json",
        )
        self.assertEqual(self.backing_assignee(via_post), self.backing_assignee(via_endpoint))

    def test_a_patch_that_does_not_touch_assignees_leaves_the_backing_task_alone(self):
        milestone_id = self.create(assignee_ids=[self.user2.id]).data["milestone"]["milestoneId"]
        self.patch(milestone_id, title="renamed")
        self.assertEqual(self.backing_assignee(milestone_id), self.user2.id)

    def test_the_backing_task_keeps_its_other_writes_after_the_second_sync(self):
        milestone_id = self.create().data["milestone"]["milestoneId"]
        res = self.patch(
            milestone_id,
            assignee_ids=[self.user2.id],
            collaborators=[self.third.id],
            custom_field_values={},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        task = MilestoneMaster.objects.get(milestone_id=milestone_id).task
        self.assertEqual({u.id for u in task.collaborators.all()}, {self.third.id})
        self.assertEqual(task.assignee_id, self.user2.id)


class AgentToolTests(BackingAssigneeBase):
    def test_manage_milestone_create_and_update(self):
        created = MANAGE_MILESTONE.run(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "Via tool",
                "assignee_ids": [str(self.user2.id)],
            },
            self.ctx,
        )
        milestone_id = created["milestone_id"]
        self.assertEqual(self.backing_assignee(milestone_id), self.user2.id)

        MANAGE_MILESTONE.run(
            {
                "action": "update",
                "milestone_id": milestone_id,
                "assignee_ids": [str(self.third.id)],
            },
            self.ctx,
        )
        self.assertEqual(self.backing_assignee(milestone_id), self.third.id)

    def test_create_task_plan_shows_the_assignee_and_writes_no_spurious_history(self):
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Plan", "assignee_ids": [str(self.user2.id)]},
                "tasks": [{"title": "One"}],
            },
            self.ctx,
        )
        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone"]["milestone_id"])
        self.assertEqual(milestone.task.assignee_id, self.user2.id)
        # The assignee really did change from the reporter to the person, and
        # that one row is true. Nothing else: no reporter row, no row from a
        # person to themselves.
        rows = self.history_rows(milestone.task)
        self.assertEqual(
            [(r.action_type, r.old_value, r.new_value) for r in rows],
            [(TaskActivityActionType.ASSIGNEE, str(self.user.id), str(self.user2.id))],
        )

    def test_create_task_plan_without_assignees_writes_no_history_either(self):
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Plan"},
                "tasks": [{"title": "One"}],
            },
            self.ctx,
        )
        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone"]["milestone_id"])
        self.assertEqual(milestone.task.assignee_id, self.user.id)
        self.assertEqual(self.history_rows(milestone.task), [])

    def test_the_tasks_in_a_plan_leave_no_such_rows_either(self):
        # The task only confirmed the milestone's backing row; check the others.
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Plan"},
                "tasks": [{"title": "One", "assignee_id": str(self.user2.id)}],
            },
            self.ctx,
        )
        from origin.models.task.task_models import TaskMaster

        task = TaskMaster.objects.get(task_id=out["tasks"][0]["task_id"])
        self.assertEqual(self.history_rows(task), [])

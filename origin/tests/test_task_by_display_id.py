"""`GET /api/v2/search/taskByDisplayId/`: one task (or milestone) by its exact display id.

The `#` menus and Spotlight's id search use it. Pins what it already did (a task by
`CODE-NUMBER`, scoped to the projects the caller can reach, 404 for everything else)
and what MIP-1040 added: a milestone row carries `milestoneId`, because a milestone
opens by its own id.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase

User = get_user_model()
URL = "/api/v2/search/taskByDisplayId/"


class TaskByDisplayIdTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate()

    def _task(self, title="Fix it", **kwargs):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title=title, **kwargs
        )
        task.refresh_from_db()  # the signal numbers it
        return task

    def _get(self, display_id, team=None):
        return self.client.get(
            URL, {"team_id": str((team or self.team).team_id), "display_id": display_id}
        )

    def test_a_task_is_found_by_its_display_id_in_any_case(self):
        task = self._task()
        for spelled in (task.display_id, task.display_id.lower(), f"#{task.display_id}"):
            res = self._get(spelled)
            self.assertEqual(res.status_code, 200, spelled)
            self.assertEqual(res.data["taskId"], task.task_id)
            self.assertEqual(res.data["displayId"], task.display_id)
            self.assertEqual(res.data["isMilestone"], False)

    def test_a_task_row_does_not_carry_a_milestone_id(self):
        res = self._get(self._task().display_id)
        self.assertNotIn("milestoneId", res.data)

    def test_a_milestone_row_carries_its_own_id(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Ship it"
        )
        backing = ensure_backing_task(milestone)
        backing.refresh_from_db()
        res = self._get(backing.display_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["isMilestone"], True)
        self.assertEqual(res.data["taskId"], backing.task_id)
        self.assertEqual(res.data["milestoneId"], milestone.milestone_id)

    def test_a_milestone_flagged_task_with_no_milestone_row_has_no_key_at_all(self):
        # An orphaned backing row (its milestone is gone): absent, not `null`, so the
        # caller falls back to the task route instead of building `/milestone/null`.
        orphan = self._task(title="orphan", is_milestone=True)
        res = self._get(orphan.display_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["isMilestone"], True)
        self.assertNotIn("milestoneId", res.data)

    def test_a_number_nobody_has_is_a_404(self):
        self._task()
        self.assertEqual(self._get("WEB-99999").status_code, 404)

    def test_a_deleted_task_is_a_404(self):
        task = self._task(is_deleted=True)
        self.assertEqual(self._get(task.display_id).status_code, 404)

    def test_a_project_the_caller_cannot_reach_is_a_404(self):
        outsider = User.objects.create_user(username="out", email="out@example.com", password="x")
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Secret",
            code="SEC",
            owner=outsider,
            project_system_user=outsider,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=outsider)
        secret = TaskMaster.objects.create(
            team=other_team, project=other_project, reporter=outsider, title="hidden"
        )
        secret.refresh_from_db()
        self.assertEqual(self._get(secret.display_id).status_code, 404)
        self.assertEqual(self._get(secret.display_id, team=other_team).status_code, 404)

    def test_a_bad_display_id_is_a_400(self):
        for bad in ("", "89", "WEB", "WEB-", "WEB-12 chips", "W-1"):
            self.assertEqual(self._get(bad).status_code, 400, repr(bad))

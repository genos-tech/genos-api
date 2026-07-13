"""Status → color resolution in the v2 task payload builders.

Regression for the "Blocked" rollout: /api/v2/task/meta/ indexed
STATUS_COLOR_MAP directly with the row's status, so the first task that
reached "Blocked" (via the dependency auto-status) 500'd the whole
endpoint with KeyError. The builders now go through
`common_color.status_color`, which (a) knows "blocked" and (b) falls
back to a neutral gray for any out-of-vocabulary value instead of
KeyErroring a listing endpoint.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.views.task.common_color import DEFAULT_STATUS_COLOR, status_color

from .test_base import BaseAPITestCase

META_URL = "/api/v2/task/meta/"


class TaskMetaStatusColorTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Website Redesign", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _get_meta(self):
        self.authenticate()
        return self.client.get(
            META_URL,
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )

    def test_blocked_task_resolves_color_instead_of_500(self):
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Stuck on the vendor",
            status="Blocked",
            assignee=self.user,
            reporter=self.user,
        )
        resp = self._get_meta()
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in resp.data if r["title"] == "Stuck on the vendor"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"]["status"], "Blocked")
        self.assertEqual(rows[0]["status"]["color"], "#e11d48")

    def test_out_of_vocabulary_status_falls_back_instead_of_500(self):
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Future status",
            status="Someday",  # not in the vocabulary
            assignee=self.user,
            reporter=self.user,
        )
        resp = self._get_meta()
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in resp.data if r["title"] == "Future status"]
        self.assertEqual(rows[0]["status"]["color"], DEFAULT_STATUS_COLOR["chipColor"])

    def test_status_color_helper_contract(self):
        self.assertEqual(status_color("Blocked")["chipColor"], "#e11d48")
        self.assertEqual(status_color("WIP")["chipColor"], "#ff8c00ff")
        self.assertEqual(status_color(None), DEFAULT_STATUS_COLOR)
        self.assertEqual(status_color("nonsense"), DEFAULT_STATUS_COLOR)

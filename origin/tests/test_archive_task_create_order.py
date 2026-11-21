"""Task create checks membership before the archive state (MIP-1884).

A stranger must not learn whether a project id is archived (409 vs 404), and a malformed project
value must never reach the database as a 500.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services.archive import archived_project_response
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
TASKS = "/api/v2/task/"


class ArchiveTaskCreateOrderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Launch",
            code="LAU",
            owner=self.user,
            project_system_user=self.user,
            archived_at=timezone.now(),
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        # user2 is on the team but NOT on the project.

    def _create(self, project):
        return self.client.post(TASKS, {"project": project, "title": "New task"}, format="json")

    def test_stranger_gets_404_for_an_archived_project(self):
        self.authenticate(self.user2)
        res = self._create(self.project.project_id)
        self.assertEqual(res.status_code, 404, res.data)

    def test_stranger_sees_same_answer_for_live_and_archived(self):
        live = ProjectMaster.objects.create(
            team=self.team,
            project_name="Live",
            code="LIV",
            owner=self.user,
            project_system_user=self.user,
        )
        self.authenticate(self.user2)
        a = self._create(self.project.project_id)
        b = self._create(live.project_id)
        self.assertEqual((a.status_code, a.data), (b.status_code, b.data))

    def test_malformed_project_is_never_a_500(self):
        self.authenticate(self.user)
        for value in ("abc", {}, [], "1; drop"):
            res = self._create(value)
            self.assertIn(res.status_code, (400, 404), (value, res.status_code))

    def test_member_still_gets_409_and_is_not_charged(self):
        self.authenticate(self.user)
        with mock.patch("origin.views.task.task_views.check_monthly_creation_quota") as quota:
            res = self._create(self.project.project_id)
        self.assertEqual(res.status_code, 409, res.data)
        quota.assert_not_called()
        self.assertFalse(TaskMaster.objects.filter(title="New task").exists())

    def test_guard_ignores_a_non_integer_id(self):
        for value in ("abc", {}, [], None, ""):
            self.assertIsNone(archived_project_response(value))

"""A task cannot be moved INTO an archived project (MIP-1885): an archive is read-only."""

from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase


class ArchiveTaskMoveDestinationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate(self.user)
        self.live = self._project("Live", "LIV")
        self.archived = self._project("Old", "OLD", archived_at=timezone.now())
        self.task = TaskMaster.objects.create(
            project=self.live, team=self.team, title="Movable", reporter=self.user
        )

    def _project(self, name, code, **extra):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            **extra,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _move(self, dest):
        return self.client.put(
            "/api/v2/task/",
            {"task_id": self.task.task_id, "project": dest.project_id},
            format="json",
        )

    def test_move_into_an_archived_project_is_refused(self):
        res = self._move(self.archived)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data["code"], "archived")
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.live.project_id)

    def test_move_between_live_projects_still_works(self):
        other = self._project("Other", "OTH")
        res = self._move(other)
        self.assertEqual(res.status_code, 200, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, other.project_id)

    def test_a_non_member_still_gets_404_not_the_archive_state(self):
        stranger_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.user2,
            project_system_user=self.user2,
            archived_at=timezone.now(),
        )
        res = self._move(stranger_project)
        self.assertEqual(res.status_code, 404, res.data)

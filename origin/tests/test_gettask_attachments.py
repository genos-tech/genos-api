"""Tests for the `?attachments=meta` opt-in on task detail endpoints
(`GET /api/v2/task/getTask/` and the thread variant).

Default mode inlines every attachment from disk as base64 inside the
JSON response — the shape the deployed frontend expects. Meta mode
must skip the disk I/O entirely and return a `file_url` the client can
lazy-load instead. These tests pin both modes and the no-disk-read
guarantee (the whole point of the option).
"""

from unittest.mock import MagicMock, mock_open, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskAttachments, TaskMaster

User = get_user_model()


class TestGetTaskAttachmentModes(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="attach-test", email="attach@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Attach Team",
            team_email="attach@team.com",
            owner=self.user,
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Attach Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Task with attachment",
            status="Open",
        )
        self.attachment = TaskAttachments.objects.create(
            task=self.task,
            attachment_id=1,
            attached_type="image/png",
            original_filename="diagram.png",
        )
        # Bypass FileField storage: point straight at a fake relative
        # path — disk access is mocked per-test.
        TaskAttachments.objects.filter(pk=self.attachment.pk).update(
            attached_file="task_attachments/diagram.png"
        )

    def _get_task(self, extra=""):
        return self.client.get(
            f"/api/v2/task/getTask/?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}&task_id={self.task.task_id}{extra}"
        )

    def test_default_mode_inlines_base64_from_disk(self):
        with patch(
            "origin.views.task.task_views.open",
            mock_open(read_data=b"png-bytes"),
            create=True,
        ) as mocked:
            resp = self._get_task()
        self.assertEqual(resp.status_code, 200)
        attachments = resp.json()[0]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertIn("file_base64", attachments[0])
        self.assertNotIn("file_url", attachments[0])
        self.assertEqual(attachments[0]["name"], "diagram.png")
        mocked.assert_called_once_with("./uploads/task_attachments/diagram.png", "rb")

    def test_meta_mode_returns_file_url_without_touching_disk(self):
        no_disk = MagicMock(side_effect=AssertionError("meta mode must not open files"))
        with patch("origin.views.task.task_views.open", no_disk, create=True):
            resp = self._get_task("&attachments=meta")
        self.assertEqual(resp.status_code, 200)
        attachments = resp.json()[0]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertNotIn("file_base64", attachments[0])
        self.assertEqual(attachments[0]["file_url"], "/media/task_attachments/diagram.png")
        self.assertEqual(attachments[0]["file"], "task_attachments/diagram.png")
        self.assertEqual(attachments[0]["attachment_id"], self.attachment.attachment_id)
        no_disk.assert_not_called()

    def test_default_mode_skips_missing_files(self):
        # FileNotFoundError on read keeps today's behavior: the entry is
        # dropped rather than failing the whole response.
        with patch(
            "origin.views.task.task_views.open",
            MagicMock(side_effect=FileNotFoundError),
            create=True,
        ):
            resp = self._get_task()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["attachments"], [])

    def test_meta_mode_lists_entries_even_when_file_is_missing_on_disk(self):
        # No disk access happens, so meta mode can't (and shouldn't)
        # filter on file existence — the URL 404s on fetch instead.
        resp = self._get_task("&attachments=meta")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()[0]["attachments"]), 1)

    def test_gettask_query_count_stays_collapsed(self):
        # Regression guard for the select_related on GetTaskView: the
        # serialization walks project / project_system_user / team /
        # assignee / reporter, which used to cost five extra queries per
        # request on the most frequently called task endpoint. Budget:
        # 1 auth-user fetch + 1 joined task row + 1 attachment prefetch +
        # 1 collaborators prefetch (one query regardless of how many
        # collaborators the task has — still O(1), never N+1).
        with self.assertNumQueries(4):
            resp = self._get_task("&attachments=meta")
        self.assertEqual(resp.status_code, 200)

"""Tests for PM (Project Message) chat endpoints."""

from django.contrib.auth import get_user_model
from rest_framework import status

from origin.models.chat.pm_models import PMMessages
from origin.models.chat.chat_master_models import UserChatMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class PMTestMixin:
    """Shared setup for PM tests that need a project with members."""

    def _create_project(self):
        self.system_user = User.objects.create_user(
            username="system_bot",
            email="bot@example.com",
            password="botpass",
            is_system_user=True,
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Test Project",
            owner=self.user,
            project_system_user=self.system_user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        UserChatMaster.objects.create(
            team=self.team,
            user=self.user,
            pinned_chats=[],
            flagged_messages=[],
        )


class PMSingleMessagePostViewTests(PMTestMixin, BaseAPITestCase):
    """POST /api/v2/pm/message/"""

    url = "/api/v2/pm/message/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self._create_project()

    def test_send_message_success(self):
        data = {
            "project_id": self.project.project_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "Hello project chat!"}],
            "task_id": None,
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["message_id"], 1)
        self.assertTrue(PMMessages.objects.filter(project=self.project, message_id=1).exists())

    def test_send_multiple_messages_increments_id(self):
        base = {
            "project_id": self.project.project_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "msg"}],
            "task_id": None,
        }
        resp1 = self.client.post(self.url, base, format="json")
        resp2 = self.client.post(self.url, base, format="json")
        self.assertEqual(resp1.data["message_id"], 1)
        self.assertEqual(resp2.data["message_id"], 2)

    def test_send_init_message_skips_if_exists(self):
        PMMessages.objects.create(
            project=self.project,
            sender=self.user,
            message_id=1,
            message_body=[{"type": "text", "text": "init"}],
        )
        data = {
            "project_id": self.project.project_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "init again"}],
            "task_id": None,
            "is_init": True,
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("message", resp.data)

    def test_send_message_unauthorized(self):
        self.unauthenticate()
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class PMSingleMessagePutViewTests(PMTestMixin, BaseAPITestCase):
    """PUT /api/v2/pm/message/ — test the fixed inverted logic for message_id/task_id."""

    url = "/api/v2/pm/message/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self._create_project()
        self.msg = PMMessages.objects.create(
            project=self.project,
            sender=self.user,
            message_id=1,
            message_body=[{"type": "text", "text": "original"}],
        )

    def test_update_by_message_id(self):
        data = {
            "project_id": self.project.project_id,
            "message_id": 1,
            "message_body": [{"type": "text", "text": "updated"}],
        }
        resp = self.client.put(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.message_body, [{"type": "text", "text": "updated"}])

    def test_update_by_task_id(self):
        """When message_id is absent, lookup should fall through to task_id."""
        from origin.models.task.task_models import TaskMaster

        task = TaskMaster.objects.create(
            project=self.project,
            title="T1",
            status="open",
        )
        self.msg.task = task
        self.msg.save()

        data = {
            "project_id": self.project.project_id,
            "task_id": task.task_id,
            "message_body": [{"type": "text", "text": "updated via task"}],
        }
        resp = self.client.put(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.message_body, [{"type": "text", "text": "updated via task"}])

    def test_update_missing_both_ids_returns_400(self):
        data = {
            "project_id": self.project.project_id,
            "message_body": [{"type": "text", "text": "bad"}],
        }
        resp = self.client.put(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_nonexistent_message_returns_404(self):
        data = {
            "project_id": self.project.project_id,
            "message_id": 9999,
            "message_body": [{"type": "text", "text": "nope"}],
        }
        resp = self.client.put(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_unauthorized(self):
        self.unauthenticate()
        resp = self.client.put(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

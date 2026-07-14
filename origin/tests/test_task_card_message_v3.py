"""Tests for `PATCH /api/v3/tasks/{task_id}/card-message/` (TaskCardMessageView).

Rewrites the PM channel's top-level "task card" header message after a
task's metadata changed, so the card body (built client-side) stays in
sync for every viewer. Restores the pre-v3 `socket.emit("message",
{methodType:"PUT"})` card-sync whose Flask handler was dropped in the v3
migration.

Auth is PM-channel membership (NOT sender-only), since the card's sender
is the project system user.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase


class TaskCardMessageViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="CardProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Old title", status="Open"
        )
        # PM channel is auto-created by the pm_channel signal on project save.
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        # Ensure the editor is a member of the PM channel (auth gate).
        ChannelMember.objects.get_or_create(
            channel=self.channel, user=self.user, defaults={"role": "member"}
        )
        # The stored task-card header, frozen with the OLD title.
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body=[{"type": "heading", "content": [{"type": "text", "text": "Old title"}]}],
            body_text="Old title",
            task_id=self.task.task_id,
        )
        self.url = reverse("v3_task_card_message", args=[self.task.task_id])

    def _new_body(self, title, mention_user_id=None):
        content = [{"type": "text", "text": title}]
        blocks = [{"type": "heading", "content": content}]
        if mention_user_id is not None:
            blocks.append(
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "props": {"userId": str(mention_user_id), "userName": "X"},
                        }
                    ],
                }
            )
        return blocks

    def test_member_rewrites_card_body(self):
        self.authenticate()
        before_ts = self.header.ts_updated_at
        resp = self.client.patch(
            self.url,
            {"body": self._new_body("New title"), "body_text": "New title"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["id"], str(self.header.id))
        self.header.refresh_from_db()
        self.assertEqual(self.header.body_text, "New title")
        self.assertEqual(self.header.body[0]["content"][0]["text"], "New title")
        # ts_updated_at bumped so the WS proxy re-broadcast + delta sync fire.
        self.assertGreater(self.header.ts_updated_at, before_ts)

    def test_metadata_is_stored_when_provided(self):
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {
                "body": self._new_body("New title"),
                "body_text": "New title",
                "metadata": {"taskId": self.task.task_id, "taskStatus": "WIP"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.header.refresh_from_db()
        self.assertEqual(self.header.metadata.get("taskStatus"), "WIP")

    def test_mentions_resynced_from_new_body(self):
        # Add user2 to the PM channel so a mention of them is a valid member.
        ChannelMember.objects.get_or_create(
            channel=self.channel, user=self.user2, defaults={"role": "member"}
        )
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {
                "body": self._new_body("New title", mention_user_id=self.user2.id),
                "body_text": "New title",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            MessageMention.objects.filter(
                message=self.header, mentioned_user_id=self.user2.id
            ).exists()
        )

    def test_no_card_message_returns_sentinel(self):
        # A task with no PM header message → 200 {"updated": false}, no error.
        orphan = TaskMaster.objects.create(
            team=self.team, project=self.project, title="No card", status="Open"
        )
        url = reverse("v3_task_card_message", args=[orphan.task_id])
        self.authenticate()
        resp = self.client.patch(
            url, {"body": self._new_body("x"), "body_text": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {"updated": False})

    def test_non_member_is_404(self):
        # user2 is not a member of this PM channel → existence-hiding 404.
        self.authenticate(self.user2)
        resp = self.client.patch(
            self.url,
            {"body": self._new_body("New title"), "body_text": "New title"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.header.refresh_from_db()
        self.assertEqual(self.header.body_text, "Old title")

    def test_missing_body_is_400(self):
        self.authenticate()
        resp = self.client.patch(self.url, {"body_text": "x"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleted_card_is_skipped(self):
        # A soft-deleted header must not be resurrected — treated as "no card".
        from django.utils import timezone

        self.header.deleted_at = timezone.now()
        self.header.save(update_fields=["deleted_at"])
        self.authenticate()
        resp = self.client.patch(
            self.url,
            {"body": self._new_body("New title"), "body_text": "New title"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {"updated": False})

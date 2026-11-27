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
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
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
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user
        )
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

    def _patch(self, **extra):
        self.authenticate()
        payload = {"body": self._new_body("Client title"), "body_text": "Client title", **extra}
        return self.client.patch(self.url, payload, format="json")

    def test_member_patch_rebuilds_the_card_from_the_row(self):
        # The row moved on without the card (a queryset update fires no signal).
        TaskMaster.objects.filter(pk=self.task.pk).update(title="New title", status="WIP")
        before_ts = self.header.ts_updated_at

        resp = self._patch(metadata={"taskStatus": "Pending", "taskId": 999})

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["id"], str(self.header.id))
        self.header.refresh_from_db()
        # What the client sent is not stored: the row is what the card says (MIP-2483).
        self.assertEqual(self.header.body_text, "New title")
        self.assertEqual(self.header.body[0]["content"][0]["text"], "🧾 New title")
        self.assertEqual(self.header.metadata["taskStatus"], "WIP")
        self.assertEqual(self.header.metadata["taskId"], self.task.task_id)
        # ts_updated_at bumped so the WS proxy re-broadcast + delta sync fire.
        self.assertGreater(self.header.ts_updated_at, before_ts)

    def test_a_card_that_already_says_the_row_is_not_written_again(self):
        self._patch()
        self.header.refresh_from_db()
        written = self.header.ts_updated_at

        resp = self._patch()

        self.assertEqual(resp.data["id"], str(self.header.id))
        self.header.refresh_from_db()
        self.assertEqual(self.header.ts_updated_at, written)

    def test_mentions_follow_the_assignee_of_the_row(self):
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user2
        )
        ChannelMember.objects.get_or_create(
            channel=self.channel, user=self.user2, defaults={"role": "member"}
        )
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.user2)

        resp = self._patch()

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

    def test_non_member_gets_the_answer_a_missing_card_gets(self):
        """MIP-1494: a non-member must not learn whether a task id has a chat card.

        Before, a card the caller cannot read answered 404 and a task with no card answered
        200 `{"updated": false}`, so any member of the team could probe task ids for cards
        (which projects have chat, which tasks were announced). Now both answer the
        sentinel the socket proxy already treats as "nothing to sync", and nothing is written.
        """
        orphan = TaskMaster.objects.create(
            team=self.team, project=self.project, title="No card", status="Open"
        )
        self.authenticate(self.user2)  # not on the project, not in the PM chat
        payload = {"body": self._new_body("New title"), "body_text": "New title"}
        refused = self.client.patch(self.url, payload, format="json")
        missing = self.client.patch(
            reverse("v3_task_card_message", args=[orphan.task_id]), payload, format="json"
        )
        self.assertEqual(refused.status_code, status.HTTP_200_OK, refused.content)
        self.assertEqual(missing.status_code, status.HTTP_200_OK, missing.content)
        self.assertEqual(refused.content, missing.content)
        self.assertEqual(refused.json(), {"updated": False})
        self.header.refresh_from_db()
        self.assertEqual(self.header.body_text, "Old title")

    def test_a_stray_channel_row_is_not_a_member_either(self):
        ChannelMember.objects.get_or_create(
            channel=self.channel, user=self.user2, defaults={"role": "member"}
        )
        self.authenticate(self.user2)
        resp = self.client.patch(
            self.url, {"body": self._new_body("Hijack"), "body_text": "Hijack"}, format="json"
        )
        self.assertEqual(resp.json(), {"updated": False})
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

"""`GET /api/v2/task/comment/` carries each comment's mirror message id (MIP-1434).

A task comment is also written as a v3 `Message` (a thread reply under the PM task header, id
`uuid5(NS, "{task_id}-{comment_id}")`), and a comment's flag and reminder are the message ones on
that row. The client has no uuid library and the namespace is a server fact, so the list hands the
id over. It is `None` when the mirror row is missing, so the client offers nothing it cannot honour.
"""

import uuid

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.unified_writer import task_comment_message_uuid
from origin.tests.test_base import BaseAPITestCase


class TaskCommentMessageIdTests(BaseAPITestCase):
    URL = "/api/v2/task/comment/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="MirrorProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        # The project owner is a PM channel member in the app (adding a project member adds the
        # channel row); a project built straight in the ORM has none, so add it.
        ChannelMember.objects.get_or_create(
            channel=self.channel, user=self.user, defaults={"role": "owner"}
        )
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )

    def _post(self, text):
        res = self.client.post(
            self.URL,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": text}]}
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["comment_id"]

    def _list(self):
        res = self.client.get(
            self.URL, {"task_id": self.task.task_id, "user_id": str(self.user.id)}
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {c["commentId"]: c for c in res.data}

    def test_each_comment_carries_its_mirror_message_id(self):
        first = self._post("one")
        second = self._post("two")
        rows = self._list()
        for comment_id in (first, second):
            expected = task_comment_message_uuid(self.task.task_id, comment_id)
            self.assertEqual(rows[comment_id]["messageId"], str(expected))
            # and it is a real row: this is the message a flag points at
            self.assertTrue(Message.objects.filter(id=expected).exists())
        self.assertNotEqual(rows[first]["messageId"], rows[second]["messageId"])

    def test_the_id_is_the_one_the_message_flag_endpoint_accepts(self):
        comment_id = self._post("flag me")
        message_id = self._list()[comment_id]["messageId"]
        res = self.client.post(f"/api/v3/messages/{message_id}/flag/")
        self.assertIn(res.status_code, (200, 201), getattr(res, "data", None))
        self.assertEqual(res.data["messageId"], message_id)

    def test_a_comment_without_a_mirror_has_no_message_id(self):
        comment_id = self._post("orphan")
        Message.objects.filter(id=task_comment_message_uuid(self.task.task_id, comment_id)).delete()
        self.assertIsNone(self._list()[comment_id]["messageId"])

    def test_a_comment_written_without_a_mirror_has_none(self):
        # A row made straight in the table (an import, a backfill gap), not through the view.
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=77, comment_body=[]
        )
        self.assertIsNone(self._list()[77]["messageId"])

    def test_the_other_fields_are_unchanged(self):
        comment_id = self._post("shape")
        row = self._list()[comment_id]
        self.assertEqual(
            sorted(row),
            sorted(
                [
                    "taskId",
                    "senderId",
                    "senderName",
                    "commentId",
                    "commentBody",
                    "messageId",
                    "reactions",
                    "tsSent",
                    "tsUpdated",
                ]
            ),
        )
        uuid.UUID(row["messageId"])  # parses

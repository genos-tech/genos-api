"""`GET /api/v2/task/comment/` carries each comment's mirror message id (MIP-1434).

A task comment is also written as a v3 `Message` (a thread reply under the PM task header, id
`uuid5(NS, "{task_id}-{comment_id}")`), and a comment's flag and reminder are the message ones on
that row. The client has no uuid library and the namespace is a server fact, so the list hands the
id over. It is `None` when the mirror row is missing, so the client offers nothing it cannot honour.
"""

import uuid
from datetime import timedelta

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
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
        # channel row); a project built straight in the ORM has neither, so add the membership
        # (its signal writes the channel row, and the project is what decides: MIP-1480).
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user
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
                    "via",  # MIP-1486
                ]
            ),
        )
        uuid.UUID(row["messageId"])  # parses


class TaskCommentMessageIdForNonMembersTests(BaseAPITestCase):
    """MIP-1442. `can_access_task` lets a task's assignee and reporter read it without a project
    membership, but the flag and reminder endpoints answer 404 for anyone who is not a member of
    the PM channel the mirror lives in. The list must not hand such a person an id to offer."""

    URL = TaskCommentMessageIdTests.URL

    def setUp(self):
        super().setUp()
        # user: a project member (and so a PM channel member). user2: only the task's assignee.
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Gate", owner=self.user, project_system_user=self.user
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="T",
            status="Open",
            reporter=self.user,
            assignee=self.user2,
        )
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body=[],
            body_text="task",
            task_id=self.task.task_id,
        )
        self.authenticate(self.user)
        res = self.client.post(
            self.URL,
            {
                "task_id": self.task.task_id,
                "comment_body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.comment_id = res.data["comment_id"]
        self.mirror_id = str(task_comment_message_uuid(self.task.task_id, self.comment_id))

    def _message_id_for(self, user, claims_to_be=None):
        self.authenticate(user)
        res = self.client.get(
            self.URL, {"task_id": self.task.task_id, "user_id": str((claims_to_be or user).id)}
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual(len(res.data), 1)
        return res.data[0]["messageId"]

    def test_a_channel_member_gets_the_id(self):
        self.assertEqual(self._message_id_for(self.user), self.mirror_id)

    def test_an_assignee_who_is_not_in_the_channel_gets_none_and_still_reads_the_comment(self):
        self.assertFalse(
            ChannelMember.objects.filter(channel=self.channel, user=self.user2).exists()
        )
        self.assertIsNone(self._message_id_for(self.user2))

    def test_the_user_id_in_the_query_string_does_not_decide(self):
        # The token is the caller. A non-member who names a member in `user_id` is still one.
        self.assertIsNone(self._message_id_for(self.user2, claims_to_be=self.user))
        self.assertEqual(self._message_id_for(self.user, claims_to_be=self.user2), self.mirror_id)

    def test_someone_who_left_the_project_gets_none(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertEqual(self._message_id_for(self.user2), self.mirror_id)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.assertIsNone(self._message_id_for(self.user2))

    def test_a_stray_channel_row_is_not_a_member_either(self):
        """MIP-1480: the flag endpoint refuses a row with no project behind it, so no id."""
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self.assertIsNone(self._message_id_for(self.user2))

    def test_none_means_the_server_would_refuse_and_an_id_means_it_accepts(self):
        # The list and the endpoints must agree, or the menu offers what silently fails.
        remind_at = (timezone.now() + timedelta(days=1)).isoformat()
        for user, offered in ((self.user, True), (self.user2, False)):
            with self.subTest(user=user.username):
                message_id = self._message_id_for(user)
                self.assertEqual(message_id is not None, offered)
                target = message_id or self.mirror_id
                flag = self.client.post(f"/api/v3/messages/{target}/flag/")
                remind = self.client.post(
                    f"/api/v3/messages/{target}/reminder/", {"remindAt": remind_at}, format="json"
                )
                if offered:
                    self.assertIn(flag.status_code, (200, 201))
                    self.assertEqual(remind.status_code, 201)
                else:
                    self.assertEqual(flag.status_code, 404)
                    self.assertEqual(remind.status_code, 404)

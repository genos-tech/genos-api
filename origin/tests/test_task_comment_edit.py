"""`PUT /api/v2/task/comment/` — editing a task comment.

The PM task thread renders a comment only through its mirror, a `Message` row
the create wrote (`write_task_comment_as_thread_reply`). The delete already
tombstones the mirror; the edit did not touch it, so a comment edited from the
task page kept its old text in the thread for good, and the two places the same
comment is shown disagreed.
"""

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.unified_writer import task_comment_message_uuid
from origin.tests.test_base import BaseAPITestCase


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


class TaskCommentEditTests(BaseAPITestCase):
    URL = "/api/v2/task/comment/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="EditProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )

    def post(self, text):
        res = self.client.post(
            self.URL,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": blocks(text),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["comment_id"]

    def put(self, comment_id, text):
        return self.client.put(
            self.URL,
            {"task_id": self.task.task_id, "comment_id": comment_id, "comment_body": blocks(text)},
            format="json",
        )

    def mirror(self, comment_id):
        return Message.objects.get(id=task_comment_message_uuid(self.task.task_id, comment_id))

    def test_an_edit_reaches_the_pm_thread(self):
        comment_id = self.post("before")
        self.assertEqual(self.mirror(comment_id).body_text, "before")

        res = self.put(comment_id, "after")

        self.assertEqual(res.status_code, 200, res.data)
        mirror = self.mirror(comment_id)
        self.assertEqual(mirror.body, blocks("after"))
        self.assertEqual(mirror.body_text, "after")
        self.assertIsNotNone(mirror.edited_at, "the thread marks an edited message")

    def test_the_edit_is_seen_by_a_client_syncing_the_thread(self):
        """The thread syncs by `ts_updated_at`, so an edit has to move it."""
        comment_id = self.post("before")
        checkpoint = timezone.now()

        self.put(comment_id, "after")

        self.assertGreater(self.mirror(comment_id).ts_updated_at, checkpoint)

    def test_the_comment_row_itself_changes_too(self):
        comment_id = self.post("before")
        self.put(comment_id, "after")
        row = TaskComments.objects.get(task=self.task.task_id, comment_id=comment_id)
        self.assertEqual(row.comment_body, blocks("after"))

    def test_a_comment_with_no_mirror_edits_without_error(self):
        """Comments from before the thread, or with dual-write off, have none."""
        comment_id = self.post("before")
        Message.objects.filter(id=task_comment_message_uuid(self.task.task_id, comment_id)).delete()
        res = self.put(comment_id, "after")
        self.assertEqual(res.status_code, 200, res.data)

    def test_a_deleted_mirror_is_left_alone(self):
        comment_id = self.post("before")
        self.client.delete(f"{self.URL}?task_id={self.task.task_id}&comment_id={comment_id}")
        before = self.mirror(comment_id)
        self.put(comment_id, "after")
        after = self.mirror(comment_id)
        self.assertEqual(after.body, before.body)
        self.assertIsNotNone(after.deleted_at)

    def test_only_the_author_can_edit(self):
        comment_id = self.post("mine")
        self.authenticate(self.user2)
        res = self.put(comment_id, "yours now")
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(self.mirror(comment_id).body_text, "mine")

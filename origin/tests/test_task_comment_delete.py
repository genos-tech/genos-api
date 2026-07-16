"""`DELETE /api/v2/task/comment/` — task-comment soft-delete.

The delete is soft on purpose, and the surrounding system already
assumed it would be (the `comment_record` signal maps an `is_deleted`
flip to COMMENT_DELETED; the `taskCommentCount` annotation and the
search chunker filter the flag). These tests pin the parts that were
missing or that a hard delete would have broken:

  * the comment disappears from GET (the filter it lacked),
  * the v3 mirror is tombstoned so the PM thread drops it too,
  * `comment_id` slots are NOT recycled — the property that makes
    soft-delete the only safe choice here,
  * repeated deletes stay idempotent (no duplicate activity row, no
    double reply_count decrement).
"""

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase


class TaskCommentDeleteTests(BaseAPITestCase):
    URL = "/api/v2/task/comment/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="DelProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        # The PM channel is auto-created by the pm_channel signal on project
        # save — reuse it (a second PM channel would violate the 1:1 unique).
        self.channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        # The task-header message the comment mirrors thread under. Without
        # it `write_task_comment_as_thread_reply` bails and there'd be no
        # mirror to assert on.
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )

    def _body(self, text):
        return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]

    def _post(self, text):
        res = self.client.post(
            self.URL,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": self._body(text),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["comment_id"]

    def _delete(self, comment_id):
        return self.client.delete(
            f"{self.URL}?task_id={self.task.task_id}&comment_id={comment_id}"
        )

    def _get(self):
        res = self.client.get(f"{self.URL}?task_id={self.task.task_id}&user_id={self.user.id}")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    # ------------------------------------------------------------------

    def test_soft_deletes_row_and_hides_it_from_get(self):
        first = self._post("first")
        self._post("second")

        res = self._delete(first)

        self.assertEqual(res.status_code, 200, res.data)
        row = TaskComments.objects.get(task=self.task.task_id, comment_id=first)
        self.assertTrue(row.is_deleted, "row should be soft-deleted, not removed")
        remaining = self._get()
        self.assertEqual([c["commentId"] for c in remaining], [2])

    def test_reports_live_count_for_the_socket_broadcast(self):
        self._post("first")
        self._post("second")
        self._post("third")

        res = self._delete(2)

        # The FE SETs the comment chip from this number, so it must be the
        # post-delete live count — not the deleted comment's id.
        self.assertEqual(res.data["taskCommentCount"], 2)
        self.assertEqual(res.data["taskId"], self.task.task_id)
        self.assertEqual(res.data["commentId"], 2)

    def test_tombstones_the_v3_mirror_and_decrements_reply_count(self):
        first = self._post("first")
        self._post("second")
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 2)

        self._delete(first)

        from origin.services.unified_writer import task_comment_message_uuid

        mirror = Message.objects.get(id=task_comment_message_uuid(self.task.task_id, first))
        self.assertIsNotNone(
            mirror.deleted_at,
            "PM thread renders comments only via the mirror — it must be tombstoned",
        )
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 1)

    def test_comment_id_slot_is_not_recycled(self):
        """The reason the delete is soft.

        `comment_id` is claimed from a row COUNT at create time and is
        unique per task. If a delete freed the row, the next comment would
        reclaim the id and collide (and its mirror uuid5 would resolve to
        the tombstoned message).
        """
        first = self._post("first")
        self._post("second")

        self._delete(first)
        third = self._post("third")

        self.assertEqual(third, 3)
        self.assertEqual(
            sorted(
                TaskComments.objects.filter(task=self.task.task_id).values_list(
                    "comment_id", flat=True
                )
            ),
            [1, 2, 3],
        )

    def test_records_a_comment_deleted_activity(self):
        first = self._post("first")

        self._delete(first)

        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
            ).count(),
            1,
        )

    def test_repeated_delete_is_idempotent(self):
        first = self._post("first")
        self._post("second")

        self.assertEqual(self._delete(first).status_code, 200)
        second_call = self._delete(first)

        self.assertEqual(second_call.status_code, 200)
        self.assertEqual(second_call.data["taskCommentCount"], 1)
        # No duplicate audit row, and the header's reply_count only ever
        # drops once for this comment.
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
            ).count(),
            1,
        )
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 1)

    def test_unknown_comment_404s(self):
        self._post("first")

        self.assertEqual(self._delete(999).status_code, 404)

    def test_missing_params_400(self):
        self.assertEqual(
            self.client.delete(f"{self.URL}?task_id={self.task.task_id}").status_code, 400
        )
        self.assertEqual(self.client.delete(f"{self.URL}?comment_id=1").status_code, 400)

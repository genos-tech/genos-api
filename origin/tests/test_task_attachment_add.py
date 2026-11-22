"""`POST /api/v2/task/attachment/`: adding a file stamps the task and tells the change feed (MIP-1595).

The delete has done this since MIP-1582 (`task_attachment_manage.delete_attachment`). Adding a file
did not: the task's `ts_updated_at` stayed where it was and the change feed heard nothing, so a
teammate's browser that syncs "tasks changed since my checkpoint" kept the old file list until
something else touched the task.

(The change feed itself was already told: the `attachment_added` history row queues a bump through
`task_progress.offer`. What was missing is the stamp, which is what a client syncing "tasks changed
since my checkpoint" compares; the feed test below pins that the feed stays told.)

Now the add shares the delete's stamp, in the same transaction as the row. The ORDER is the mirror
of the delete's, and chosen for the same reason (fail safe): the file is written to storage first (the
FileField does it as the row is saved), then the row, then the stamp, all in one transaction. A
failure at any step leaves at worst an ORPHAN FILE nobody can reach (removed on a best-effort basis),
never a listed row whose file is missing.

There is no agent or MCP tool that adds a file to a task (the agent can list and delete them only), so
the page's route is the only writer.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.serializers.task.task_serializers import TaskAttachmentsSerializer
from origin.services import change_feed, task_attachment_manage
from origin.tests.test_agent_parity_attachments import URL, AttachmentsBase

User = get_user_model()


class Boom(Exception):
    pass


def _upload(name="a.txt", content=b"hello"):
    return SimpleUploadedFile(name, content, content_type="text/plain")


class AddStampTests(AttachmentsBase):
    def rest_add(self, task=None, user=None, name="a.txt"):
        self.sign_in(user or self.user)
        with self.captureOnCommitCallbacks(execute=self.commit_hooks):
            return self.client.post(
                URL,
                {
                    "task": (task or self.task).task_id,
                    "attachment_id": -1,
                    "attached_type": "text/plain",
                    "attached_file": _upload(name),
                },
                format="multipart",
            )

    def old_stamp(self, task):
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=task.ts_updated_at.replace(year=2020)
        )
        return TaskMaster.objects.get(pk=task.pk).ts_updated_at

    def test_the_task_is_stamped_so_a_browser_syncing_since_its_checkpoint_hears_of_it(self):
        before = self.old_stamp(self.task)
        self.assertEqual(self.rest_add().status_code, 201)
        self.assertGreater(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)

    def test_the_change_feed_is_told_the_projects_tasks_changed(self):
        self.sign_in(self.user)
        change_feed.reset_pending()
        self.commit_hooks = False  # the feed's keys are flushed by the commit hooks
        with self.captureOnCommitCallbacks(execute=False):
            res = self.client.post(
                URL,
                {
                    "task": self.task.task_id,
                    "attachment_id": -1,
                    "attached_type": "text/plain",
                    "attached_file": _upload(),
                },
                format="multipart",
            )
            self.assertEqual(res.status_code, 201)
            keys = change_feed.pending_keys()
        self.assertIn(change_feed.tasks_project_key(self.project.project_id), keys)
        self.assertIn(change_feed.tasks_team_key(self.team.team_id), keys)

    def test_the_other_tasks_are_left_alone(self):
        other = self.make_task("Other")
        before = TaskMaster.objects.get(pk=other.pk).ts_updated_at
        self.assertEqual(self.rest_add().status_code, 201)
        self.assertEqual(TaskMaster.objects.get(pk=other.pk).ts_updated_at, before)

    def test_the_history_row_is_still_written_once_and_the_response_is_unchanged(self):
        res = self.rest_add()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            set(res.data),
            {
                "id",
                "task",
                "attachment_id",
                "attached_file",
                "attached_type",
                "original_filename",
                "ts_created_at",
                "ts_updated_at",
                "file_base64",
                "size_bytes",
                "name",
            },
        )
        rows = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.ATTACHMENT_ADDED
        )
        self.assertEqual(rows.count(), 1)

    def test_a_refused_upload_stamps_and_tells_nobody(self):
        before = self.old_stamp(self.task)
        self.sign_in(self.user)
        change_feed.reset_pending()
        # no file at all: the serializer refuses it
        res = self.client.post(
            URL,
            {"task": self.task.task_id, "attachment_id": -1, "attached_type": "text/plain"},
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)
        self.assertEqual(change_feed.pending_keys(), set())

    def test_a_stranger_stamps_nothing_and_sees_a_missing_task(self):
        stranger = User.objects.create_user(username="s", email="s@example.com", password="x")
        before = self.old_stamp(self.task)
        self.assertEqual(self.rest_add(user=stranger).status_code, 404)
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)


class AddOrderTests(AttachmentsBase):
    def serializer(self, number=1):
        return TaskAttachmentsSerializer(
            data={
                "task": self.task.task_id,
                "attachment_id": number,
                "attached_file": _upload("n.txt"),
                "attached_type": "text/plain",
                "original_filename": "n.txt",
            }
        )

    def test_a_rollback_keeps_no_row_and_stamps_and_tells_nobody(self):
        before = TaskMaster.objects.get(pk=self.task.pk).ts_updated_at
        change_feed.reset_pending()
        serializer = self.serializer()
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.captureOnCommitCallbacks(execute=False):
            with self.assertRaises(Boom), transaction.atomic():
                task_attachment_manage.add_attachment(serializer)
                raise Boom
        self.assertFalse(TaskAttachments.objects.filter(task=self.task).exists())
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)

    def test_a_stamp_that_fails_leaves_no_row_and_no_file(self):
        serializer = self.serializer()
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with (
            mock.patch.object(task_attachment_manage, "_stamp_task", side_effect=Boom),
            self.assertRaises(Boom),
        ):
            task_attachment_manage.add_attachment(serializer)
        self.assertFalse(TaskAttachments.objects.filter(task=self.task).exists())
        self.assertEqual(self.files_on_disk(), set())

    def test_a_row_that_will_not_save_leaves_no_file(self):
        serializer = self.serializer()
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with (
            mock.patch.object(TaskAttachments, "save", side_effect=Boom),
            self.assertRaises(Boom),
        ):
            task_attachment_manage.add_attachment(serializer)
        self.assertFalse(TaskAttachments.objects.filter(task=self.task).exists())

    def test_a_file_that_will_not_be_cleaned_up_is_logged_and_the_error_still_raised(self):
        serializer = self.serializer()
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with (
            mock.patch.object(task_attachment_manage, "_stamp_task", side_effect=Boom),
            mock.patch(
                "django.db.models.fields.files.FieldFile.delete", side_effect=OSError("disk")
            ),
            self.assertLogs("origin.services.task_attachment_manage", level="WARNING"),
            self.assertRaises(Boom),
        ):
            task_attachment_manage.add_attachment(serializer)

    def test_a_good_add_keeps_the_file_the_row_points_at(self):
        serializer = self.serializer()
        self.assertTrue(serializer.is_valid(), serializer.errors)
        row = task_attachment_manage.add_attachment(serializer)
        self.assertTrue(default_storage.exists(row.attached_file.name))
        self.assertEqual(TaskAttachments.objects.filter(task=self.task).count(), 1)

"""`DELETE /api/v2/task/attachment/` and the ids of a task's files (MIP-1582).

What the delete did wrong, and what these pin now:

  * ORDER. The file left storage first, outside any transaction: a failure between the two steps
    left a row pointing at nothing (a broken download for everyone). The row goes first, inside a
    transaction, and the file is removed only once that has COMMITTED. The worst a failure can
    leave is now an orphan file nobody can reach (logged), never a listed file that is gone.
  * STALE CACHES. Nothing told teammates' browsers: the task's `updated_at` did not move and the
    change feed heard nothing, so their copy kept listing the file until something else touched the
    task.
  * 204 CARRIED A BODY. A 204 has none.
  * A DELETED TASK. The view opted out of the soft-delete refusal (MIP-1572) "until this task
    decided". It answers as a task that never existed, like every other attachment route and like
    the agent's tool: a deleted task's media is unreadable and its files stay as they are.
  * ID REUSE. The next id was highest-plus-one over the live rows, so a deleted id came back for the
    next upload, and a stale tab (or an approval that waited a long time) deleted the replacement.

THE RULE, as it has always been and is now written down: anyone who can open the task (project
member, assignee, reporter) may delete any of its files, whoever added it. `TaskAttachments` has no
uploader column; the history row `attachment_added` names who did it.
"""

from unittest import mock

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.services import change_feed, task_attachment_manage
from origin.tests.test_agent_parity_attachments import URL, AttachmentsBase


class Boom(Exception):
    pass


class DeleteOrderTests(AttachmentsBase):
    def test_the_file_is_removed_only_after_the_row_delete_has_committed(self):
        attachment = self.attach(self.task, "n.txt")
        name = attachment.attached_file.name
        self.commit_hooks = False  # the commit is what this test steps through by hand
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)
            # the row is gone, the file is not yet: it waits for the commit
            self.assertFalse(TaskAttachments.objects.filter(pk=attachment.pk).exists())
            self.assertTrue(default_storage.exists(name))
        self.assertTrue(callbacks)
        for callback in callbacks:
            callback()
        self.assertFalse(default_storage.exists(name))

    def test_a_rollback_keeps_both_the_row_and_the_file(self):
        attachment = self.attach(self.task, "n.txt")
        name, pk = attachment.attached_file.name, attachment.pk  # `delete()` clears the pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(Boom), transaction.atomic():
                task_attachment_manage.delete_attachment(attachment)
                raise Boom
        self.assertEqual(callbacks, [])
        self.assertTrue(TaskAttachments.objects.filter(pk=pk).exists())
        self.assertTrue(default_storage.exists(name))

    def test_a_storage_failure_after_the_commit_leaves_no_row_and_says_so_in_the_log(self):
        attachment = self.attach(self.task, "private-name.txt")
        with (
            # outermost, so the callbacks the next one runs on its way out are still logged
            self.assertLogs("origin.services.task_attachment_manage", level="WARNING") as logs,
            mock.patch(
                "django.db.models.fields.files.FieldFile.delete", side_effect=OSError("disk")
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)
        self.assertFalse(TaskAttachments.objects.filter(pk=attachment.pk).exists())
        self.assertIn("attachment_id=1", "\n".join(logs.output))
        self.assertNotIn("private-name", "\n".join(logs.output))

    def test_a_row_that_will_not_delete_leaves_the_file_where_it_is(self):
        attachment = self.attach(self.task, "n.txt")
        name = attachment.attached_file.name
        with (
            mock.patch.object(TaskAttachments, "delete", side_effect=Boom),
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(Boom),
        ):
            task_attachment_manage.delete_attachment(attachment)
        self.assertTrue(default_storage.exists(name))
        self.assertTrue(TaskAttachments.objects.filter(pk=attachment.pk).exists())

    def test_a_row_with_no_file_is_still_deleted(self):
        attachment = TaskAttachments.objects.create(task=self.task, attachment_id=1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)
        self.assertFalse(TaskAttachments.objects.filter(pk=attachment.pk).exists())


class StaleCacheTests(AttachmentsBase):
    def test_the_task_is_stamped_so_a_browser_syncing_since_its_checkpoint_hears_of_it(self):
        self.attach(self.task, "n.txt")
        TaskMaster.objects.filter(pk=self.task.pk).update(
            ts_updated_at=self.task.ts_updated_at.replace(year=2020)
        )
        before = TaskMaster.objects.get(pk=self.task.pk).ts_updated_at
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)
        self.assertGreater(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)

    def test_the_change_feed_is_told_the_projects_tasks_changed(self):
        self.attach(self.task, "n.txt")
        change_feed.reset_pending()
        self.commit_hooks = False  # the feed's keys are flushed by the commit hooks
        with self.captureOnCommitCallbacks(execute=False):
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)
            keys = change_feed.pending_keys()
        self.assertIn(change_feed.tasks_project_key(self.project.project_id), keys)
        self.assertIn(change_feed.tasks_team_key(self.team.team_id), keys)

    def test_a_rollback_stamps_and_tells_nobody(self):
        attachment = self.attach(self.task, "n.txt")
        before = TaskMaster.objects.get(pk=self.task.pk).ts_updated_at
        change_feed.reset_pending()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with self.assertRaises(Boom), transaction.atomic():
                task_attachment_manage.delete_attachment(attachment)
                raise Boom
        self.assertEqual(callbacks, [])
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, before)

    def test_the_other_tasks_are_left_alone(self):
        other = self.make_task("Other")
        self.attach(self.task, "n.txt")
        before = TaskMaster.objects.get(pk=other.pk).ts_updated_at
        with self.captureOnCommitCallbacks(execute=True):
            self.rest_delete(self.task.task_id, 1)
        self.assertEqual(TaskMaster.objects.get(pk=other.pk).ts_updated_at, before)

    def test_the_history_row_is_still_written_once(self):
        self.attach(self.task, "n.txt")
        with self.captureOnCommitCallbacks(execute=True):
            self.rest_delete(self.task.task_id, 1)
        rows = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.ATTACHMENT_REMOVED
        )
        self.assertEqual(rows.count(), 1)


class ResponseTests(AttachmentsBase):
    def test_a_204_carries_no_body(self):
        self.attach(self.task, "n.txt")
        with self.captureOnCommitCallbacks(execute=True):
            res = self.rest_delete(self.task.task_id, 1)
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.content, b"")
        self.assertIsNone(res.data)


class DeletedTaskTests(AttachmentsBase):
    def test_a_deleted_task_answers_as_a_task_that_never_existed(self):
        gone = self.make_task("Gone")
        attachment = self.attach(gone, "a.txt")
        TaskMaster.objects.filter(pk=gone.pk).update(is_deleted=True)
        res = self.rest_delete(gone.task_id, 1)
        missing = self.rest_delete(987654, 1)
        self.assertEqual((res.status_code, res.data), (404, {"error": "Task not found."}))
        self.assertEqual((missing.status_code, missing.data), (res.status_code, res.data))
        self.assertTrue(TaskAttachments.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(default_storage.exists(attachment.attached_file.name))

    def test_a_live_task_is_not_refused_that_way(self):
        self.attach(self.task, "n.txt")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.rest_delete(self.task.task_id, 1).status_code, 204)


class IdsAreNotReusedTests(AttachmentsBase):
    def upload(self, name="u.txt", user=None):
        self.sign_in(user or self.user)
        return self.client.post(
            URL,
            {
                "task": str(self.task.task_id),
                "attachment_id": "-1",
                "attached_type": "text/plain",
                "attached_file": SimpleUploadedFile(name, b"bytes"),
            },
            format="multipart",
        )

    def test_a_deleted_id_does_not_come_back_for_the_next_upload(self):
        first = self.upload("one.txt")
        second = self.upload("two.txt")
        self.assertEqual(
            (first.status_code, second.status_code, first.data["attachment_id"]), (201, 201, 1)
        )
        self.assertEqual(second.data["attachment_id"], 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.rest_delete(self.task.task_id, 2).status_code, 204)
        third = self.upload("three.txt")
        self.assertEqual(third.status_code, 201, third.data)
        self.assertEqual(third.data["attachment_id"], 3)

    def test_a_stale_delete_of_the_old_number_finds_nothing_and_leaves_the_new_file(self):
        self.upload("one.txt")
        with self.captureOnCommitCallbacks(execute=True):
            self.rest_delete(self.task.task_id, 1)
        replacement = self.upload("replacement.txt")
        self.assertEqual(replacement.data["attachment_id"], 2)
        res = self.rest_delete(self.task.task_id, 1)  # the tab that still lists file 1
        self.assertEqual(res.status_code, 404)
        self.assertEqual(
            list(
                TaskAttachments.objects.filter(task=self.task).values_list(
                    "attachment_id", flat=True
                )
            ),
            [2],
        )

    def test_the_first_file_of_a_task_is_still_number_one(self):
        self.assertEqual(self.upload().data["attachment_id"], 1)

    def test_it_holds_for_the_highest_id_too_and_per_task(self):
        other = self.make_task("Other")
        self.attach(other, "x.txt")  # task B has a file #1, which says nothing about task A
        self.assertEqual(self.upload().data["attachment_id"], 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.rest_delete(self.task.task_id, 1)
        self.assertEqual(self.upload().data["attachment_id"], 2)

    def test_the_agent_s_delete_leaves_the_same_gap(self):
        self.attach(self.task, "one.txt")
        with self.captureOnCommitCallbacks(execute=True):
            self.run_tool(self.task, 1)
        self.assertEqual(self.upload().data["attachment_id"], 2)

    def test_files_added_before_the_history_existed_keep_the_old_numbering(self):
        """No history row to remember a number by (a file from before the signal, or history a
        retention period has since cleared): the live rows still decide, as they always did."""
        self.attach(self.task, "one.txt")
        with self.captureOnCommitCallbacks(execute=True):
            self.rest_delete(self.task.task_id, 1)
        TaskActivity.objects.filter(task=self.task).delete()
        self.assertEqual(self.upload().data["attachment_id"], 1)

    def test_the_numbering_helper_reads_both(self):
        self.assertEqual(task_attachment_manage.next_attachment_id(self.task.task_id), 1)
        a = self.attach(self.task, "one.txt")
        self.attach(self.task, "two.txt")
        self.assertEqual(task_attachment_manage.next_attachment_id(self.task.task_id), 3)
        with self.captureOnCommitCallbacks(execute=True):
            task_attachment_manage.delete_attachment(a)
        self.assertEqual(task_attachment_manage.next_attachment_id(self.task.task_id), 3)


class WhoMayDeleteTests(AttachmentsBase):
    """The rule written down: anyone who can open the task, whoever added the file."""

    def test_a_member_deletes_a_file_a_colleague_added(self):
        self.attach(self.task, "theirs.txt", by=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.rest_delete(self.task.task_id, 1, user=self.user2)
        self.assertEqual(res.status_code, 204)

    def test_the_uploader_is_named_in_the_history_not_on_the_row(self):
        self.assertNotIn("uploader", {f.name for f in TaskAttachments._meta.get_fields()})
        self.assertNotIn("sender", {f.name for f in TaskAttachments._meta.get_fields()})

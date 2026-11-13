"""`repair_task_comment_mirrors`: find the task comments that have no v3 mirror, and write it (MIP-1440).

Pinned here:

  * the default run only reports, and writes nothing;
  * `--apply` writes the mirror with the id the live path uses, under the task's header, keeps
    the COMMENT's sent time, counts it in the header's reply count, and a second run does nothing;
  * it raises no activity (those comments are old: telling people now would be noise);
  * soft-deleted comments are not looked at; a tombstoned mirror of a live comment is reported
    and left alone; a comment whose project has no PM channel is reported as unmirrorable;
  * `--task-id`, `--project-id` and `--limit` narrow it;
  * a mirror the writer cannot write is reported and the run fails after trying the rest.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.task_comment_fanout import fan_out_task_comment
from origin.services.unified_writer import task_comment_message_uuid
from origin.tests.test_base import BaseAPITestCase

COMMAND = "repair_task_comment_mirrors"


class RepairTaskCommentMirrorsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project("RepairProj")
        self.task = self._task(self.project, "T")

    def _project(self, name):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        channel = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        ChannelMember.objects.get_or_create(
            channel=channel, user=self.user, defaults={"role": "owner"}
        )
        return project

    def _task(self, project, title):
        # A second person is the assignee, so the live fan-out of a comment has someone to tell
        # (the test that no activity is raised would be vacuous without one).
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title=title,
            status="Open",
            reporter=self.user,
            assignee=self.user2,
        )
        channel = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        seq = (Message.objects.filter(channel=channel).count()) + 1
        Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=seq,
            body=[],
            body_text=title,
            task_id=task.task_id,
        )
        return task

    def _comment(self, task, comment_id, *, sent_days_ago=3, is_deleted=False):
        """A comment saved straight into the table: no fan-out, so no mirror (what a failed
        mirror leaves behind)."""
        comment = TaskComments.objects.create(
            task=task,
            sender=self.user,
            comment_id=comment_id,
            comment_body=[{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}],
            is_deleted=is_deleted,
        )
        sent = timezone.now() - timedelta(days=sent_days_ago)
        TaskComments.objects.filter(pk=comment.pk).update(ts_sent_at=sent)
        comment.ts_sent_at = sent
        return comment

    def _mirror(self, task, comment_id):
        return Message.objects.filter(id=task_comment_message_uuid(task.task_id, comment_id))

    def _run(self, *args):
        out = StringIO()
        call_command(COMMAND, *args, stdout=out)
        return out.getvalue()

    def _header(self, task):
        return Message.objects.get(task_id=task.task_id, is_thread_reply=False)

    def test_the_default_run_reports_and_writes_nothing(self):
        self._comment(self.task, 1)
        before = Message.objects.count()
        out = self._run()
        self.assertIn("missing:      1", out)
        self.assertIn("Report only", out)
        self.assertEqual(Message.objects.count(), before)
        self.assertFalse(self._mirror(self.task, 1).exists())

    def test_apply_writes_the_mirror_the_live_path_would_have(self):
        comment = self._comment(self.task, 1, sent_days_ago=5)
        out = self._run("--apply")
        self.assertIn("Written: 1; failed: 0", out)
        mirror = self._mirror(self.task, 1).get()
        header = self._header(self.task)
        self.assertEqual(mirror.parent_id, header.id)
        self.assertTrue(mirror.is_thread_reply)
        self.assertEqual(mirror.metadata, {"taskCommentId": 1})
        self.assertEqual(mirror.task_id, self.task.task_id)
        self.assertEqual(mirror.sender_id, self.user.id)
        self.assertEqual(mirror.body, comment.comment_body)
        header.refresh_from_db()
        self.assertEqual(header.reply_count, 1)

    def test_the_mirror_keeps_the_comments_own_sent_time(self):
        # Thread replies are read in ts_sent_at order: an old comment must not sort last.
        comment = self._comment(self.task, 1, sent_days_ago=30)
        self._run("--apply")
        self.assertEqual(self._mirror(self.task, 1).get().ts_sent_at, comment.ts_sent_at)

    def test_a_second_run_finds_nothing_and_changes_nothing(self):
        self._comment(self.task, 1)
        self._run("--apply")
        count = Message.objects.count()
        out = self._run("--apply")
        self.assertIn("missing:      0", out)
        self.assertIn("Written: 0; failed: 0", out)
        self.assertEqual(Message.objects.count(), count)
        self.assertEqual(self._header(self.task).reply_count, 1)

    def test_it_raises_no_activity(self):
        self._comment(self.task, 1)
        before = Activity.objects.count()
        self._run("--apply")
        self.assertEqual(Activity.objects.count(), before)
        # The control: the live path, on the same kind of comment, does tell the assignee.
        live = self._comment(self.task, 2)
        fan_out_task_comment(live)
        self.assertGreater(Activity.objects.count(), before)

    def test_a_task_with_no_header_gets_one_from_the_writer(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="No card", status="Open"
        )
        self._comment(task, 1)
        self._run("--apply")
        self.assertTrue(self._mirror(task, 1).exists())
        self.assertTrue(
            Message.objects.filter(task_id=task.task_id, is_thread_reply=False).exists()
        )

    def test_replies_are_written_in_comment_order(self):
        # Saved out of order on purpose: the table's own order would give 3, 1, 2.
        for comment_id in (3, 1, 2):
            self._comment(self.task, comment_id)
        self._run("--apply")
        seqs = [self._mirror(self.task, c).get().seq for c in (1, 2, 3)]
        self.assertEqual(seqs, sorted(seqs))

    def test_a_soft_deleted_comment_is_not_looked_at(self):
        self._comment(self.task, 1, is_deleted=True)
        out = self._run("--apply")
        self.assertIn("looked at: 0", out)
        self.assertFalse(self._mirror(self.task, 1).exists())

    def test_a_tombstoned_mirror_of_a_live_comment_is_reported_and_left_alone(self):
        self._comment(self.task, 1)
        self._run("--apply")
        when = timezone.now()
        self._mirror(self.task, 1).update(deleted_at=when)
        reply_count = self._header(self.task).reply_count
        out = self._run("--apply")
        self.assertIn("tombstoned:   1", out)
        self.assertIn("missing:      0", out)
        self.assertEqual(self._mirror(self.task, 1).get().deleted_at, when)
        self.assertEqual(self._header(self.task).reply_count, reply_count)

    def test_a_comment_whose_project_has_no_pm_channel_is_unmirrorable(self):
        project = self._project("NoChannel")
        task = self._task(project, "T2")
        self._comment(task, 1)
        Message.objects.filter(task_id=task.task_id).delete()
        Channel.objects.filter(project_id=project.project_id, kind=ChannelKind.PM).delete()
        out = self._run("--apply")
        self.assertIn("unmirrorable: 1", out)
        self.assertIn("missing:      0", out)
        self.assertIn("Written: 0; failed: 0", out)

    def test_task_id_and_project_id_narrow_the_run(self):
        other_project = self._project("Other")
        other = self._task(other_project, "T3")
        sibling = self._task(self.project, "T4")
        for task in (self.task, other, sibling):
            self._comment(task, 1)

        self._run("--apply", "--task-id", str(self.task.task_id))
        self.assertTrue(self._mirror(self.task, 1).exists())
        self.assertFalse(self._mirror(other, 1).exists())
        self.assertFalse(self._mirror(sibling, 1).exists())

        self._run("--apply", "--project-id", str(other_project.project_id))
        self.assertTrue(self._mirror(other, 1).exists())
        self.assertFalse(self._mirror(sibling, 1).exists())

    def test_limit_caps_how_many_are_written(self):
        for comment_id in (1, 2, 3):
            self._comment(self.task, comment_id)
        out = self._run("--apply", "--limit", "2")
        self.assertIn("Written: 2; failed: 0", out)
        self.assertEqual(
            [self._mirror(self.task, c).exists() for c in (1, 2, 3)], [True, True, False]
        )

    def test_a_mirror_the_writer_cannot_write_fails_the_run_but_not_the_rest(self):
        self._comment(self.task, 1)
        self._comment(self.task, 2)
        from origin.services import unified_writer

        original = unified_writer.write_task_comment_as_thread_reply

        def flaky(**kwargs):
            if kwargs["comment_id"] == 1:
                return None
            return original(**kwargs)

        out = StringIO()
        with mock.patch(
            "origin.management.commands.repair_task_comment_mirrors."
            "write_task_comment_as_thread_reply",
            side_effect=flaky,
        ):
            with self.assertRaises(CommandError):
                call_command(COMMAND, "--apply", stdout=out)
        self.assertIn("failed: task", out.getvalue())
        self.assertFalse(self._mirror(self.task, 1).exists())
        self.assertTrue(self._mirror(self.task, 2).exists())

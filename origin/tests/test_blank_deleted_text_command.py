"""MIP-1784: `blank_deleted_text`, the one-off clean-up of rows deleted before the delete blanked.

    python manage.py blank_deleted_text                # DRY RUN: counts only, writes nothing
    python manage.py blank_deleted_text --apply --yes  # blank them

What it pins: nothing is written without `--apply --yes`; only DELETED rows are touched; the
tombstone (id, seq, sender, `deleted_at`) stays; `ts_updated_at` moves so a connected client
re-reads the row and the search index re-chunks the channel; a deleted comment whose mirror
message was left live is tombstoned with it; a second run finds nothing; and the output is counts
only, never a word of a message.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageMention,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.chunkers.chat_chunker import iter_gm_chunks
from origin.services import unified_writer
from origin.tests.test_base import BaseAPITestCase

SECRET = "words-a-person-deleted-long-ago"


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def run(*args):
    out = StringIO()
    call_command("blank_deleted_text", *args, stdout=out)
    return out.getvalue()


class BlankDeletedTextCommand(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="chat", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self._seq = 0
        self.old = timezone.now() - timedelta(days=139)

    def say(self, text=SECRET, *, deleted=False, root=None, **extra):
        self._seq += 1
        message = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=self._seq,
            body=_blocks(text),
            body_text=text,
            thread_root=root,
            parent=root,
            is_thread_reply=root is not None,
            **extra,
        )
        if deleted:
            Message.objects.filter(pk=message.pk).update(
                deleted_at=self.old, ts_updated_at=self.old
            )
            message.refresh_from_db()
        return message

    # -- safety ------------------------------------------------------------------------------

    def test_the_default_is_a_dry_run_that_writes_nothing(self):
        gone = self.say(deleted=True)

        out = run()

        gone.refresh_from_db()
        self.assertEqual(gone.body_text, SECRET)
        self.assertEqual(gone.ts_updated_at, self.old)
        self.assertIn("DRY RUN", out)
        self.assertIn("1 deleted message", out)
        self.assertNotIn(SECRET, out)

    def test_apply_needs_yes(self):
        self.say(deleted=True)
        with self.assertRaises(CommandError):
            run("--apply")
        self.assertEqual(Message.objects.get().body_text, SECRET)

    def test_a_negative_batch_size_is_refused(self):
        with self.assertRaises(CommandError):
            run("--batch-size", "0")

    # -- messages ----------------------------------------------------------------------------

    def test_apply_blanks_deleted_messages_and_keeps_the_tombstone(self):
        gone = self.say(deleted=True, metadata={"taskId": 3, "note": "x"})
        MessageMention.objects.create(message=gone, mentioned_user=self.user2)
        MessageAttachment.objects.create(
            message=gone, uploader=self.user, file="chats/a/b.bin", original_name="cv.pdf"
        )

        out = run("--apply", "--yes")

        gone.refresh_from_db()
        self.assertEqual((gone.body, gone.body_text), ([], ""))
        self.assertEqual(gone.metadata, {"taskId": 3})
        self.assertEqual(gone.deleted_at, self.old)
        self.assertEqual((gone.sender_id, gone.seq), (self.user.id, 1))
        self.assertGreater(gone.ts_updated_at, self.old, "clients must re-read the row")
        self.assertFalse(MessageMention.objects.filter(message=gone).exists())
        self.assertEqual(MessageAttachment.objects.get(message=gone).original_name, "")
        self.assertIn("APPLY", out)
        self.assertNotIn(SECRET, out)

    def test_a_live_message_is_never_touched(self):
        live = self.say("keep me")
        gone = self.say(deleted=True)

        run("--apply", "--yes")

        live.refresh_from_db()
        self.assertEqual(live.body_text, "keep me")
        self.assertEqual(live.body, _blocks("keep me"))
        self.assertIsNone(live.deleted_at)
        gone.refresh_from_db()
        self.assertEqual(gone.body_text, "")

    def test_a_row_the_retention_policy_already_blanked_is_not_rewritten(self):
        gone = self.say(deleted=True)
        Message.objects.filter(pk=gone.pk).update(body={}, body_text="")

        out = run("--apply", "--yes")

        gone.refresh_from_db()
        self.assertEqual(gone.ts_updated_at, self.old)
        self.assertIn("0 deleted message", out)

    def test_a_second_run_finds_nothing(self):
        self.say(deleted=True)
        run("--apply", "--yes")
        out = run("--apply", "--yes")
        self.assertIn("0 deleted message", out)

    def test_batches_cover_every_row(self):
        rows = [self.say(f"{SECRET}-{i}", deleted=True) for i in range(5)]
        run("--apply", "--yes", "--batch-size", "2")
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.body_text, "")

    def test_the_search_index_picks_the_channel_up_again(self):
        """The reindexer re-chunks a channel with a message updated since its last pass."""
        self.say("live")
        self.say(deleted=True)
        Message.objects.update(ts_updated_at=self.old)
        since = timezone.now() - timedelta(minutes=1)
        self.assertEqual(list(iter_gm_chunks(since=since)), [], "nothing is dirty before")

        run("--apply", "--yes")

        entities = [b.entity_id for b in iter_gm_chunks(since=since)]
        self.assertEqual(entities, [f"gm:{self.channel.id}"])

    # -- task comments -----------------------------------------------------------------------

    def comment_with_mirror(self, *, mirror_deleted):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        task = TaskMaster.objects.create(team=self.team, project=project, title="T", status="Open")
        pm = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        header = Message.objects.create(
            channel=pm, sender=self.user, seq=1, body=[], body_text="", task_id=task.task_id
        )
        comment = TaskComments.objects.create(
            task=task, sender=self.user, comment_id=1, comment_body=_blocks(SECRET)
        )
        mirror = unified_writer.write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=1,
            sender_id=str(self.user.id),
            body=_blocks(SECRET),
            project_id=project.project_id,
            bypass_flag=True,
        )
        self.assertIsNotNone(mirror)
        header.refresh_from_db()
        self.assertEqual(header.reply_count, 1)
        # The comment was deleted before this change: flag only.
        TaskComments.objects.filter(pk=comment.pk).update(is_deleted=True)
        if mirror_deleted:
            Message.objects.filter(pk=mirror.pk).update(deleted_at=self.old)
            Message.objects.filter(pk=header.pk).update(reply_count=0)
        return task, comment, mirror, header

    def test_apply_blanks_a_deleted_comment_and_its_mirror_together(self):
        _task, comment, mirror, header = self.comment_with_mirror(mirror_deleted=True)

        out = run("--apply", "--yes")

        comment.refresh_from_db()
        mirror.refresh_from_db()
        self.assertTrue(comment.is_deleted)
        self.assertEqual(comment.comment_body, [])
        self.assertEqual((mirror.body, mirror.body_text), ([], ""))
        self.assertEqual(mirror.metadata.get("taskCommentId"), 1)
        self.assertIn("1 deleted task comment", out)
        self.assertNotIn(SECRET, out)

    def test_a_mirror_left_live_by_an_old_delete_is_tombstoned_with_its_comment(self):
        _task, comment, mirror, header = self.comment_with_mirror(mirror_deleted=False)

        out = run("--apply", "--yes")

        mirror.refresh_from_db()
        header.refresh_from_db()
        self.assertIsNotNone(mirror.deleted_at)
        self.assertEqual((mirror.body, mirror.body_text), ([], ""))
        self.assertEqual(header.reply_count, 0, "counted down once, as a delete does")
        self.assertIn("1 comment mirror", out)

    def test_a_live_comment_and_its_mirror_are_never_touched(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        task = TaskMaster.objects.create(team=self.team, project=project, title="T", status="Open")
        pm = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=pm, sender=self.user, seq=1, body=[], body_text="", task_id=task.task_id
        )
        comment = TaskComments.objects.create(
            task=task, sender=self.user, comment_id=1, comment_body=_blocks("fine")
        )
        mirror = unified_writer.write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=1,
            sender_id=str(self.user.id),
            body=_blocks("fine"),
            project_id=project.project_id,
            bypass_flag=True,
        )

        run("--apply", "--yes")

        comment.refresh_from_db()
        mirror.refresh_from_db()
        self.assertEqual(comment.comment_body, _blocks("fine"))
        self.assertEqual(mirror.body_text, "fine")
        self.assertIsNone(mirror.deleted_at)

"""MIP-1802: a `ThreadSummary` goes with the thread it summarises, and with the chat.

`ThreadSummary` is the cached LLM digest of a thread's REPLIES. It has no foreign key (`chat_id` and
`thread_id` are bare UUID columns), so no delete cascaded to it: a deleted reply, a deleted thread
root, a deleted chat (soft or hard), a deleted project's chat and a torn-down demo team all left the
digest text behind, and its search chunk with it.

The rule these tests pin: when a message of a thread is deleted (root or reply), that thread's
summary row and chunk go (the next "ask about this thread" writes a fresh one from what is left);
when a chat is deleted, every summary of the chat goes in the same transaction; and
`prune_orphaned_thread_summaries` finds, dry-run by default and counts only, the rows that
existing deletions already left.

Every case also checks that ANOTHER thread's and ANOTHER chat's summary survives.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.models import RagChunk, ThreadSummary
from origin.services import channel_delete, message_tombstone
from origin.services.demo_seeder import delete_demo_team_data
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase

SECRET = "the-digest-of-what-was-said-in-the-thread"

_BULK = "origin.search_engine.purge.os_helpers.bulk"
_CLIENT = "origin.search_engine.purge.get_client"


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class Fixture(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self.make_channel("chat")
        self.other_channel = self.make_channel("other chat")

    def make_channel(self, title, team=None, kind=ChannelKind.GM, **extra):
        channel = Channel.objects.create(
            team=team or self.team, kind=kind, title=title, owner=self.user, **extra
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        return channel

    def say(self, channel, text="hello", *, root=None):
        seq = Message.objects.filter(channel=channel).count() + 1
        return Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=seq,
            body=_blocks(text),
            body_text=text,
            thread_root=root,
            parent=root,
            is_thread_reply=root is not None,
        )

    def thread(self, channel, replies=2):
        """A root with `replies` replies, and the summary of the thread. Returns (root, [replies], row)."""
        root = self.say(channel, "root")
        kids = [self.say(channel, f"reply {i}", root=root) for i in range(replies)]
        return root, kids, self.summary(channel, root)

    def summary(self, channel, root, *, kind=None, team=None):
        kind = int(channel.kind) if kind is None else kind
        row = ThreadSummary.objects.create(
            team_id=str(team or channel.team_id),
            chat_type=kind,
            chat_id=channel.id,
            thread_id=root.id,
            summary_text=SECRET,
        )
        self.chunk(row)
        return row

    def chunk(self, row):
        eid = f"thread_summary:{row.chat_type}:{row.chat_id}:{row.thread_id}"
        return RagChunk.objects.create(
            chunk_id=eid,
            entity_type="thread_summary",
            entity_id=eid,
            chunk_type="x",
            team_id=self.team.team_id,
            text_hash="h",
            embedding_model="m",
        )

    def has_row(self, row):
        return ThreadSummary.objects.filter(pk=row.pk).exists()

    def has_chunk(self, row):
        return RagChunk.objects.filter(
            entity_type="thread_summary",
            entity_id=f"thread_summary:{row.chat_type}:{row.chat_id}:{row.thread_id}",
        ).exists()

    def commit(self):
        """Run the on-commit work (search purge), with OpenSearch stubbed out."""
        return _Commit(self)


class _Commit:
    def __init__(self, case):
        self.case = case

    def __enter__(self):
        self.patches = [mock.patch(_BULK, return_value=(0, [])), mock.patch(_CLIENT)]
        for p in self.patches:
            p.start()
        self.ctx = self.case.captureOnCommitCallbacks(execute=True)
        self.ctx.__enter__()

    def __exit__(self, *exc):
        self.ctx.__exit__(*exc)
        for p in self.patches:
            p.stop()


class DeletingAMessageForgetsItsThreadSummary(Fixture):
    def setUp(self):
        super().setUp()
        self.root, self.kids, self.row = self.thread(self.channel)
        self.other_root, _, self.other_row = self.thread(self.channel)
        _, _, self.elsewhere = self.thread(self.other_channel)

    def untouched(self):
        self.assertTrue(self.has_row(self.other_row))
        self.assertTrue(self.has_chunk(self.other_row))
        self.assertTrue(self.has_row(self.elsewhere))
        self.assertTrue(self.has_chunk(self.elsewhere))

    def test_deleting_the_thread_root_removes_its_summary_and_chunk(self):
        self.authenticate()
        with self.commit():
            res = self.client.delete(f"/api/v3/messages/{self.root.id}/")
        self.assertEqual(res.status_code, 204)

        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_chunk(self.row))
        self.untouched()

    def test_deleting_a_reply_removes_the_summary_that_quotes_it(self):
        """The digest is made of the replies, so a deleted reply's text lives in it."""
        self.authenticate()
        with self.commit():
            res = self.client.delete(f"/api/v3/messages/{self.kids[0].id}/")
        self.assertEqual(res.status_code, 204)

        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_chunk(self.row))
        self.untouched()

    def test_every_writer_goes_through_the_tombstone(self):
        with self.commit():
            message_tombstone.tombstone_message(Message.objects.get(pk=self.kids[1].pk))
        self.assertFalse(self.has_row(self.row))
        self.untouched()

    def test_deleting_a_message_of_a_thread_with_no_summary_changes_nothing(self):
        bare_root = self.say(self.channel, "no summary")
        bare_reply = self.say(self.channel, "x", root=bare_root)
        with self.commit():
            message_tombstone.tombstone_message(bare_reply)
        self.assertTrue(self.has_row(self.row))
        self.untouched()

    def test_a_message_of_the_same_thread_id_in_another_chat_is_not_matched(self):
        """The row is keyed by the chat as well as the thread."""
        stranger = self.say(self.other_channel, "elsewhere")
        with self.commit():
            message_tombstone.tombstone_message(stranger)
        self.assertTrue(self.has_row(self.row))
        self.untouched()

    def test_the_tombstone_costs_one_query_when_the_thread_has_no_summary(self):
        """Message delete is a hot path: the summary lookup is ONE read, no write, no purge."""
        root = self.say(self.channel, "bare")
        reply = self.say(self.channel, "bare reply", root=root)
        with CaptureQueriesContext(connection) as ctx:
            message_tombstone.tombstone_message(reply)
        table = ThreadSummary._meta.db_table
        touching = [q["sql"] for q in ctx.captured_queries if table in q["sql"]]
        self.assertEqual(len(touching), 1, touching)
        self.assertTrue(touching[0].lstrip().upper().startswith("SELECT"), touching)

    def test_a_blank_of_a_legacy_deleted_message_forgets_the_summary_too(self):
        """`blank_deleted_text` goes through `tombstone_message`: an old delete is cleaned and so
        is the digest that quotes it."""
        now = timezone.now()
        Message.objects.filter(pk=self.kids[0].pk).update(deleted_at=now, ts_updated_at=now)
        out = StringIO()
        call_command("blank_deleted_text", "--apply", "--yes", stdout=out)
        self.assertFalse(self.has_row(self.row))
        self.untouched()


class DeletingAChatForgetsItsSummaries(Fixture):
    def setUp(self):
        super().setUp()
        self.root, _, self.row = self.thread(self.channel)
        self.root2, _, self.row2 = self.thread(self.channel)
        _, _, self.elsewhere = self.thread(self.other_channel)

    def check(self):
        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_row(self.row2))
        self.assertFalse(self.has_chunk(self.row))
        self.assertFalse(self.has_chunk(self.row2))
        self.assertTrue(self.has_row(self.elsewhere))
        self.assertTrue(self.has_chunk(self.elsewhere))

    def test_soft_delete(self):
        with self.commit():
            channel_delete.delete_group_chat(self.channel, mode=channel_delete.SOFT)
        self.assertTrue(Channel.objects.filter(pk=self.channel.pk, is_deleted=True).exists())
        self.check()

    def test_hard_delete(self):
        with self.commit():
            channel_delete.delete_group_chat(self.channel, mode=channel_delete.HARD)
        self.assertFalse(Channel.objects.filter(pk=self.channel.pk).exists())
        self.check()

    def test_a_failed_hard_delete_keeps_the_summaries(self):
        with mock.patch.object(
            channel_delete, "record_security_event", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                channel_delete.delete_group_chat(self.channel, mode=channel_delete.HARD)
        self.assertTrue(self.has_row(self.row))
        self.assertTrue(self.has_row(self.row2))

    def test_a_plain_channel_delete_forgets_them_too(self):
        """Nothing else may leave a row behind: the demo teardown, the eval harness and any later
        path delete a `Channel` directly."""
        Message.objects.filter(channel=self.channel).delete()
        with self.commit():
            self.channel.delete()
        self.assertFalse(ThreadSummary.objects.filter(chat_id=self.channel.id).exists())
        self.assertTrue(self.has_row(self.elsewhere))


class DeletingAProjectForgetsItsChatSummaries(Fixture):
    def test_the_project_chat_summaries_go_and_another_projects_stay(self):
        def project(name, code):
            p = ProjectMaster.objects.create(
                team=self.team,
                project_name=name,
                code=code,
                owner=self.user,
                project_system_user=self.user,
            )
            ProjectMembers.objects.create(team=self.team, project=p, attendee=self.user)
            return p

        doomed, kept = project("Doomed", "DOO"), project("Kept", "KEP")
        doomed_chat = Channel.objects.get(project=doomed, kind=ChannelKind.PM)
        kept_chat = Channel.objects.get(project=kept, kind=ChannelKind.PM)
        _, _, gone = self.thread(doomed_chat)
        _, _, stays = self.thread(kept_chat)

        with self.commit():
            delete_project(doomed)

        self.assertFalse(self.has_row(gone))
        self.assertFalse(self.has_chunk(gone))
        self.assertTrue(self.has_row(stays))
        self.assertTrue(self.has_chunk(stays))


class DemoTeardownForgetsSummaries(Fixture):
    def test_a_demo_team_teardown_removes_its_summaries_and_only_its_own(self):
        owner = self.user2
        demo = TeamMaster.objects.create(
            team_name="Demo 1802", team_email="demo1802@example.com", owner=owner, is_demo=True
        )
        TeamMembers.objects.create(team=demo, attendee=owner)
        demo_chat = Channel.objects.create(team=demo, kind=ChannelKind.GM, title="d", owner=owner)
        ChannelMember.objects.create(channel=demo_chat, user=owner, role="owner")
        root = Message.objects.create(
            channel=demo_chat, sender=owner, seq=1, body=_blocks("r"), body_text="r"
        )
        _, _, stays = self.thread(self.channel)
        gone = ThreadSummary.objects.create(
            team_id=str(demo.team_id),
            chat_type=int(ChannelKind.GM),
            chat_id=demo_chat.id,
            thread_id=root.id,
            summary_text=SECRET,
        )

        with mock.patch(_BULK, return_value=(0, [])), mock.patch(_CLIENT):
            delete_demo_team_data(demo.team_id)

        self.assertFalse(self.has_row(gone))
        self.assertTrue(self.has_row(stays))


class PruneOrphanedThreadSummaries(Fixture):
    """The one-off repair: rows the deletes above did not forget because they ran before."""

    def setUp(self):
        super().setUp()
        _, _, self.healthy = self.thread(self.channel)

        # Chat gone: the row names a chat that no longer exists.
        self.ghost = ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=2,
            chat_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
            summary_text=SECRET,
        )
        self.chunk(self.ghost)

        # Chat soft-deleted.
        soft = self.make_channel("soft", is_deleted=True)
        _, _, self.chat_deleted = self.thread(soft)

        # Thread root gone.
        self.root_gone_root = self.say(self.channel, "to be removed")
        self.root_gone = self.summary(self.channel, self.root_gone_root)
        self.say(self.channel, "orphan reply", root=self.root_gone_root)
        Message.objects.filter(thread_root=self.root_gone_root).delete()
        Message.objects.filter(pk=self.root_gone_root.pk).delete()

        # Every reply deleted (the root is live).
        _, kids, self.all_deleted = self.thread(self.channel)
        for kid in kids:
            Message.objects.filter(pk=kid.pk).update(deleted_at=timezone.now())

        # A reply deleted AFTER the digest was made, another one live: the digest quotes it.
        _, kids, self.stale = self.thread(self.channel)
        past = timezone.now() - timedelta(days=1)
        ThreadSummary.objects.filter(pk=self.stale.pk).update(ts_updated_at=past)
        Message.objects.filter(pk=kids[0].pk).update(deleted_at=timezone.now())

        # A reply deleted BEFORE the digest was made: the digest is clean.
        _, kids, self.regenerated = self.thread(self.channel)
        Message.objects.filter(pk=kids[0].pk).update(deleted_at=timezone.now() - timedelta(days=2))

        self.orphans = [
            self.ghost,
            self.chat_deleted,
            self.root_gone,
            self.all_deleted,
            self.stale,
        ]
        self.kept = [self.healthy, self.regenerated]

    def run_command(self, *args):
        out = StringIO()
        with mock.patch(_BULK, return_value=(0, [])), mock.patch(_CLIENT):
            call_command("prune_orphaned_thread_summaries", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_counts_by_reason_and_writes_nothing(self):
        text = self.run_command()

        self.assertIn("DRY RUN", text)
        for reason, n in (
            ("chat_missing", 1),
            ("chat_deleted", 1),
            ("thread_missing", 1),
            ("no_live_reply", 1),
            ("text_of_deleted_message", 1),
        ):
            self.assertIn(f"{reason}={n}", text)
        self.assertIn("5", text)
        for row in self.orphans + self.kept:
            self.assertTrue(self.has_row(row))
        self.assertTrue(self.has_chunk(self.ghost))

    def test_output_is_counts_only(self):
        for args in ((), ("--apply",)):
            text = self.run_command(*args)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(str(self.channel.id), text)
            self.assertNotIn(str(self.ghost.chat_id), text)
            self.assertNotIn(str(self.ghost.thread_id), text)

    def test_apply_removes_the_orphans_and_their_chunks_and_only_them(self):
        self.run_command("--apply")

        for row in self.orphans:
            self.assertFalse(self.has_row(row), row.pk)
        self.assertFalse(self.has_chunk(self.ghost))
        for row in self.kept:
            self.assertTrue(self.has_row(row), row.pk)
            self.assertTrue(self.has_chunk(row), row.pk)

    def test_a_second_apply_has_nothing_to_do(self):
        self.run_command("--apply")
        text = self.run_command("--apply")
        self.assertIn("0 ", text)
        for row in self.kept:
            self.assertTrue(self.has_row(row))


class EditingAMessageForgetsItsThreadSummary(Fixture):
    """MIP-1819: the old text of an edited reply is no longer what its author wants kept."""

    def setUp(self):
        super().setUp()
        self.root, self.kids, self.row = self.thread(self.channel)
        _, _, self.other_row = self.thread(self.channel)
        _, _, self.elsewhere = self.thread(self.other_channel)

    def untouched(self):
        for row in (self.other_row, self.elsewhere):
            self.assertTrue(self.has_row(row))
            self.assertTrue(self.has_chunk(row))

    def patch(self, message, text):
        self.authenticate()
        with self.commit():
            return self.client.patch(
                f"/api/v3/messages/{message.id}/",
                {"body": _blocks(text), "body_text": text},
                format="json",
            )

    def test_editing_a_reply_removes_the_summary_and_chunk(self):
        res = self.patch(self.kids[0], "changed")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_chunk(self.row))
        self.untouched()

    def test_editing_the_root_removes_the_summary_and_chunk(self):
        res = self.patch(self.root, "changed root")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_chunk(self.row))
        self.untouched()

    def test_an_edit_that_keeps_the_text_keeps_the_summary(self):
        res = self.patch(self.kids[0], self.kids[0].body_text)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(self.has_row(self.row))
        self.assertTrue(self.has_chunk(self.row))
        self.untouched()

    def test_a_failed_edit_keeps_the_summary(self):
        self.authenticate()
        with self.commit():
            res = self.client.patch(
                f"/api/v3/messages/{self.kids[0].id}/", {"body": "nope"}, format="json"
            )
        self.assertEqual(res.status_code, 400)
        self.assertTrue(self.has_row(self.row))

    def test_the_task_comment_mirror_edit_forgets_it(self):
        from origin.services import unified_writer

        mirror = Message.objects.create(
            id=unified_writer.task_comment_message_uuid(7, 9),
            channel=self.channel,
            sender=self.user,
            seq=900,
            body=_blocks("old"),
            body_text="old",
            thread_root=self.root,
            parent=self.root,
            is_thread_reply=True,
        )
        with self.commit():
            unified_writer.update_task_comment_thread_reply(
                task_id=7, comment_id=9, body=_blocks("new")
            )
        self.assertFalse(self.has_row(self.row))
        self.assertFalse(self.has_chunk(self.row))
        self.untouched()
        self.assertEqual(Message.objects.get(pk=mirror.pk).body_text, "new")

    def test_the_task_comment_mirror_edit_with_the_same_text_keeps_it(self):
        from origin.services import unified_writer

        Message.objects.create(
            id=unified_writer.task_comment_message_uuid(7, 9),
            channel=self.channel,
            sender=self.user,
            seq=900,
            body=_blocks("same"),
            body_text="same",
            thread_root=self.root,
            parent=self.root,
            is_thread_reply=True,
        )
        with self.commit():
            unified_writer.update_task_comment_thread_reply(
                task_id=7, comment_id=9, body=_blocks("same")
            )
        self.assertTrue(self.has_row(self.row))

    def test_the_card_rewrite_forgets_the_thread_summary(self):
        from origin.views.chat.message_views import rewrite_card_message

        with self.commit():
            rewrite_card_message(
                Message.objects.get(pk=self.root.pk), body=_blocks("card v2"), body_text="card v2"
            )
        self.assertFalse(self.has_row(self.row))
        self.untouched()

    def test_the_mirror_of_a_legacy_thread_edit_forgets_it(self):
        from origin.services import unified_writer

        with self.commit():
            with (
                mock.patch.object(unified_writer, "_is_enabled", return_value=True),
                mock.patch.object(unified_writer, "_resolve_channel", return_value=self.channel),
                mock.patch.object(unified_writer, "_resolve_message", return_value=self.root),
            ):
                unified_writer.write_thread_message(
                    chat_type=2,
                    chat_id=0,
                    thread_id=0,
                    message_id=self.kids[0].seq,
                    sender_id=self.user.id,
                    body=_blocks("legacy edit"),
                )
        self.assertFalse(self.has_row(self.row))
        self.untouched()

    def test_an_edit_costs_one_summary_read_when_the_thread_has_none(self):
        root = self.say(self.channel, "bare")
        reply = self.say(self.channel, "bare reply", root=root)
        self.authenticate()
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.patch(
                f"/api/v3/messages/{reply.id}/",
                {"body": _blocks("e"), "body_text": "e"},
                format="json",
            )
        self.assertEqual(res.status_code, 200, res.content)
        table = ThreadSummary._meta.db_table
        touching = [q["sql"] for q in ctx.captured_queries if table in q["sql"]]
        self.assertEqual(len(touching), 1, touching)
        self.assertTrue(touching[0].lstrip().upper().startswith("SELECT"), touching)


class PruneCountsSummariesOlderThanAnEdit(Fixture):
    def test_the_dry_run_counts_a_digest_older_than_an_edit_and_writes_nothing(self):
        _, kids, stale = self.thread(self.channel)
        _, _, fresh = self.thread(self.channel)
        _, kids2, _ = self.thread(self.channel)
        ThreadSummary.objects.filter(pk=stale.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=1)
        )
        Message.objects.filter(pk=kids[0].pk).update(edited_at=timezone.now())
        # edited BEFORE the digest was made: the digest is clean
        Message.objects.filter(pk=kids2[0].pk).update(edited_at=timezone.now() - timedelta(days=2))
        out = StringIO()
        call_command("prune_orphaned_thread_summaries", stdout=out)
        text = out.getvalue()
        self.assertIn("edited_since_digest=1", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(ThreadSummary.objects.count(), 3)
        self.assertTrue(self.has_row(fresh))

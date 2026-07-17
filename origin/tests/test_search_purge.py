"""Tests for `origin.search_engine.purge` + the deleted-content chunker fixes.

The purge module is the delete path for the OpenSearch index: chunkers
filter deleted rows out of iteration, so a deleted entity is never
regenerated and `ingestion._delete_stale` never fires for it. These
tests cover:

  * `purge_entities` / `purge_chunks` — exact removal (OpenSearch bulk
    mocked; RagChunk assertions are real).
  * the best-effort `purge_*` view hooks — never raise.
  * `sweep_orphans` — per-type liveness: dead entities purged, live
    entities kept, unparseable / unknown-type ids kept (fail-safe).
  * task_chunker — soft-deleting a comment now marks the task dirty for
    incremental passes (the dirty query must NOT filter `is_deleted`).
  * chat_chunker — entities whose live messages are all gone are yielded
    as empty tombstones (so ingestion purges their chunks) instead of
    being silently skipped.

OpenSearch is never touched: `purge.os_helpers.bulk` and
`purge.get_client` are patched; the sweep/purge logic under test is the
RagChunk bookkeeping + liveness resolution, which is all DB-driven.
"""

import datetime as dt
import uuid
from unittest import mock

from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import purge
from origin.search_engine.chunkers.base import CHAT_TYPE_DM
from origin.search_engine.chunkers.chat_chunker import iter_dm_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.models import AgentRun, NoteSummary, RagChunk, ThreadSummary
from origin.tests.test_base import BaseAPITestCase


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class PurgeTestCase(BaseAPITestCase):
    """Base: mocks the OpenSearch client/bulk and tracks RagChunk rows."""

    def setUp(self):
        super().setUp()
        self.bulk_mock = mock.MagicMock(return_value=(0, []))
        p1 = mock.patch.object(purge.os_helpers, "bulk", self.bulk_mock)
        p2 = mock.patch.object(purge, "get_client", return_value=mock.MagicMock())
        p3 = mock.patch.object(purge, "get_index_alias", return_value="test-alias")
        p1.start(), p2.start(), p3.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(p3.stop)

    def _chunk(self, chunk_id, entity_type, entity_id):
        return RagChunk.objects.create(
            chunk_id=chunk_id,
            entity_type=entity_type,
            entity_id=entity_id,
            chunk_type="test",
            team_id=self.team.team_id,
            text_hash="h",
            embedding_model="m",
        )

    def _bulk_deleted_ids(self):
        ids = set()
        for call in self.bulk_mock.call_args_list:
            for action in call.args[1]:
                self.assertEqual(action["_op_type"], "delete")
                ids.add(action["_id"])
        return ids


class TestPurgeCore(PurgeTestCase):
    def test_purge_entities_removes_tracking_and_issues_deletes(self):
        self._chunk("task:1:title_content", "task", "task:1")
        self._chunk("task:1:comment:2", "task", "task:1")
        self._chunk("task:9:title_content", "task", "task:9")

        n = purge.purge_entities([("task", "task:1")])

        self.assertEqual(n, 2)
        self.assertEqual(
            set(RagChunk.objects.values_list("chunk_id", flat=True)),
            {"task:9:title_content"},
        )
        self.assertEqual(
            self._bulk_deleted_ids(), {"task:1:title_content", "task:1:comment:2"}
        )

    def test_purge_chunks_keeps_tracking_rows_on_transport_failure(self):
        self._chunk("task:1:title_content", "task", "task:1")
        self.bulk_mock.side_effect = RuntimeError("opensearch down")
        with self.assertRaises(RuntimeError):
            purge.purge_chunks(["task:1:title_content"])
        # Tracking row survives so the sweep can retry later.
        self.assertTrue(RagChunk.objects.filter(chunk_id="task:1:title_content").exists())

    def test_purge_note_also_purges_note_summary_entity(self):
        self._chunk("note:personal:5:sec:0", "note", "note:personal:5")
        self._chunk("note_summary:1:5", "note_summary", "note_summary:1:5")
        purge.purge_note("personal", 5)
        self.assertEqual(RagChunk.objects.count(), 0)

    def test_purge_todo_item_suffix_does_not_match_other_items(self):
        self._chunk("todo:2026-07-17:item:5", "todo", "todo:2026-07-17:item:5")
        self._chunk("todo:2026-07-17:item:15", "todo", "todo:2026-07-17:item:15")
        purge.purge_todo_item(5)
        self.assertEqual(
            set(RagChunk.objects.values_list("chunk_id", flat=True)),
            {"todo:2026-07-17:item:15"},
        )

    def test_hooks_swallow_exceptions(self):
        self.bulk_mock.side_effect = RuntimeError("opensearch down")
        self._chunk("task:1:title_content", "task", "task:1")
        # None of these may raise — a purge failure must not fail a delete.
        purge.purge_task(1)
        purge.purge_milestone(1)
        purge.purge_note("personal", 1)
        purge.purge_todo_item(1)
        purge.purge_task_comment(1, 1)


class TestSweepOrphans(PurgeTestCase):
    def _sweep_purged(self, **kwargs):
        stats = purge.sweep_orphans(**kwargs)
        return stats, set(RagChunk.objects.values_list("entity_id", flat=True))

    def test_task_liveness(self):
        live = TaskMaster.objects.create(team=self.team, title="live", status="Open")
        soft = TaskMaster.objects.create(
            team=self.team, title="soft", status="Open", is_deleted=True
        )
        self._chunk("a", "task", f"task:{live.task_id}")
        self._chunk("b", "task", f"task:{soft.task_id}")
        self._chunk("c", "task", "task:999999")  # hard-deleted

        stats, remaining = self._sweep_purged()

        self.assertEqual(remaining, {f"task:{live.task_id}"})
        self.assertEqual(stats["purged_by_type"]["task"], 2)

    def test_milestone_and_note_liveness(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user
        )
        ms = MilestoneMaster.objects.create(team=self.team, project=project, title="ms")
        gone_ms = MilestoneMaster.objects.create(
            team=self.team, project=project, title="x", is_deleted=True
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=_bn("hi")
        )
        self._chunk("a", "milestone", f"milestone:{ms.milestone_id}")
        self._chunk("b", "milestone", f"milestone:{gone_ms.milestone_id}")
        self._chunk("c", "note", f"note:personal:{note.note_id}")
        self._chunk("d", "note", "note:personal:999999")

        _, remaining = self._sweep_purged()

        self.assertEqual(
            remaining,
            {f"milestone:{ms.milestone_id}", f"note:personal:{note.note_id}"},
        )

    def test_note_summary_needs_note_and_summary_row(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=_bn("hi")
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=1,
            note_id=note.note_id,
            summary_text="s",
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=1,
            note_id=999999,  # note itself is gone
            summary_text="s",
        )
        self._chunk("a", "note_summary", f"note_summary:1:{note.note_id}")
        self._chunk("b", "note_summary", "note_summary:1:999999")
        # Note exists but the summary row does not.
        note2 = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n2", body=_bn("hi")
        )
        self._chunk("c", "note_summary", f"note_summary:1:{note2.note_id}")

        _, remaining = self._sweep_purged()

        self.assertEqual(remaining, {f"note_summary:1:{note.note_id}"})

    def test_chat_liveness_channel_and_thread(self):
        live_ch = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        dead_ch = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM, is_deleted=True)
        ChannelMember.objects.create(channel=live_ch, user=self.user)

        # Live root + live reply → thread entity live.
        threaded_root = Message.objects.create(
            channel=live_ch, sender=self.user, seq=1, body=_bn("r")
        )
        Message.objects.create(
            channel=live_ch,
            sender=self.user,
            seq=2,
            body=_bn("reply"),
            is_thread_reply=True,
            thread_root=threaded_root,
            parent=threaded_root,
        )
        # Live root with no live replies: it's a plain main-timeline
        # message now — its old thread entity is dead.
        plain_root = Message.objects.create(
            channel=live_ch, sender=self.user, seq=3, body=_bn("p")
        )
        # Root soft-deleted but a live reply keeps the thread alive.
        anchorless_root = Message.objects.create(
            channel=live_ch,
            sender=self.user,
            seq=4,
            body=_bn("a"),
            deleted_at=timezone.now(),
        )
        Message.objects.create(
            channel=live_ch,
            sender=self.user,
            seq=5,
            body=_bn("reply"),
            is_thread_reply=True,
            thread_root=anchorless_root,
            parent=anchorless_root,
        )

        self._chunk("a", "chat", f"dm:{live_ch.id}")
        self._chunk("b", "chat", f"dm:{dead_ch.id}")
        self._chunk("c", "chat", f"dm:{live_ch.id}:thread:{threaded_root.id}")
        self._chunk("d", "chat", f"dm:{live_ch.id}:thread:{plain_root.id}")
        self._chunk("e", "chat", f"dm:{live_ch.id}:thread:{anchorless_root.id}")

        _, remaining = self._sweep_purged()

        self.assertEqual(
            remaining,
            {
                f"dm:{live_ch.id}",
                f"dm:{live_ch.id}:thread:{threaded_root.id}",
                f"dm:{live_ch.id}:thread:{anchorless_root.id}",
            },
        )

    def test_thread_summary_needs_row_channel_and_live_thread(self):
        ch = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        root = Message.objects.create(channel=ch, sender=self.user, seq=1, body=_bn("r"))
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=ch.id,
            thread_id=root.id,
            summary_text="s",
        )
        self._chunk("a", "thread_summary", f"thread_summary:{CHAT_TYPE_DM}:{ch.id}:{root.id}")
        # No ThreadSummary row for this one.
        other_root = Message.objects.create(channel=ch, sender=self.user, seq=2, body=_bn("o"))
        self._chunk(
            "b", "thread_summary", f"thread_summary:{CHAT_TYPE_DM}:{ch.id}:{other_root.id}"
        )
        # Summary row exists but every message in the thread is deleted.
        dead_root = Message.objects.create(
            channel=ch, sender=self.user, seq=3, body=_bn("x"), deleted_at=timezone.now()
        )
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=ch.id,
            thread_id=dead_root.id,
            summary_text="derived from deleted messages",
        )
        self._chunk(
            "c", "thread_summary", f"thread_summary:{CHAT_TYPE_DM}:{ch.id}:{dead_root.id}"
        )

        _, remaining = self._sweep_purged()

        self.assertEqual(
            remaining, {f"thread_summary:{CHAT_TYPE_DM}:{ch.id}:{root.id}"}
        )

    def test_todo_and_agent_run_liveness(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=dt.date(2026, 7, 17)
        )
        item = ToDoItem.objects.create(group=group, title="t")
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q"
        )
        self._chunk("a", "todo", f"todo:2026-07-17:item:{item.item_id}")
        self._chunk("b", "todo", "todo:2026-07-17:item:999999")
        self._chunk("c", "conversation", f"conversation:{run.run_id}")
        self._chunk("d", "conversation", f"conversation:{uuid.uuid4()}")
        self._chunk("e", "spotlight_answer", f"spotlight_answer:{run.run_id}")
        self._chunk("f", "spotlight_answer", f"spotlight_answer:{uuid.uuid4()}")

        _, remaining = self._sweep_purged()

        self.assertEqual(
            remaining,
            {
                f"todo:2026-07-17:item:{item.item_id}",
                f"conversation:{run.run_id}",
                f"spotlight_answer:{run.run_id}",
            },
        )

    def test_unknown_type_and_unparseable_ids_are_kept(self):
        self._chunk("a", "future_lane", "future_lane:1")
        self._chunk("b", "task", "task:not-a-number")
        self._chunk("c", "chat", "not-a-chat-id")

        stats, remaining = self._sweep_purged()

        self.assertEqual(remaining, {"future_lane:1", "task:not-a-number", "not-a-chat-id"})
        self.assertEqual(stats["kept_unknown_type"], 1)
        self.assertEqual(stats["kept_unparseable"], 2)
        self.assertEqual(stats["entities_purged"], 0)

    def test_dry_run_purges_nothing_but_counts(self):
        self._chunk("a", "task", "task:999999")
        self._chunk("b", "task", "task:999999")

        stats, remaining = self._sweep_purged(dry_run=True)

        self.assertEqual(remaining, {"task:999999"})
        self.assertEqual(stats["entities_purged"], 1)
        self.assertEqual(stats["chunks_purged"], 2)
        self.bulk_mock.assert_not_called()


class TestTaskChunkerCommentDeleteDirty(BaseAPITestCase):
    def test_soft_deleted_comment_marks_task_dirty_and_drops_chunk(self):
        task = TaskMaster.objects.create(team=self.team, title="T", status="Open")
        comment = TaskComments.objects.create(
            task=task, sender=self.user, comment_id=1, comment_body=_bn("note this")
        )
        since = timezone.now()

        comment.is_deleted = True
        comment.save(update_fields=["is_deleted", "ts_updated_at"])

        batches = [b for b in iter_task_chunks(since=since)]
        # The deletion alone must re-emit the task (dirty via the comment's
        # ts_updated_at bump) with the comment chunk gone, so ingestion
        # purges it as stale.
        self.assertEqual([b.entity_id for b in batches], [f"task:{task.task_id}"])
        chunk_ids = {c.chunk_id for c in batches[0].chunks}
        self.assertNotIn(f"task:{task.task_id}:comment:1", chunk_ids)


class TestThreadSummaryChunkerTombstone(BaseAPITestCase):
    def test_summary_of_fully_deleted_thread_is_tombstoned(self):
        from origin.search_engine.chunkers.thread_summary_chunker import (
            iter_thread_summary_chunks,
        )

        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user)
        root = Message.objects.create(
            channel=dm, sender=self.user, seq=1, body=_bn("r"), deleted_at=timezone.now()
        )
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=dm.id,
            thread_id=root.id,
            summary_text="derived from deleted messages",
        )

        batches = list(iter_thread_summary_chunks())

        self.assertEqual(
            [b.entity_id for b in batches],
            [f"thread_summary:{CHAT_TYPE_DM}:{dm.id}:{root.id}"],
        )
        self.assertEqual(batches[0].chunks, [])


class TestChatChunkerTombstones(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user2)

    def test_fully_deleted_channel_history_yields_empty_tombstone(self):
        msg = Message.objects.create(
            channel=self.dm, sender=self.user, seq=1, body=_bn("secret")
        )
        since = timezone.now()
        msg.deleted_at = timezone.now()
        msg.save(update_fields=["deleted_at", "ts_updated_at"])

        batches = [b for b in iter_dm_chunks(since=since) if f"{self.dm.id}" in b.entity_id]

        self.assertEqual([b.entity_id for b in batches], [f"dm:{self.dm.id}"])
        self.assertEqual(batches[0].chunks, [])

    def test_last_thread_reply_deleted_yields_empty_thread_tombstone(self):
        root = Message.objects.create(channel=self.dm, sender=self.user, seq=1, body=_bn("r"))
        reply = Message.objects.create(
            channel=self.dm,
            sender=self.user,
            seq=2,
            body=_bn("secret reply"),
            is_thread_reply=True,
            thread_root=root,
            parent=root,
        )
        since = timezone.now()
        reply.deleted_at = timezone.now()
        reply.save(update_fields=["deleted_at", "ts_updated_at"])

        batches = list(iter_dm_chunks(since=since))
        by_id = {b.entity_id: b for b in batches}
        # The root is a plain main-timeline message again (no live replies
        # root a thread), and no thread entity remains — the root no longer
        # roots anything. Main entity keeps the root's chunk.
        self.assertIn(f"dm:{self.dm.id}", by_id)
        main_chunk_ids = {c.chunk_id for c in by_id[f"dm:{self.dm.id}"].chunks}
        self.assertIn(f"chat:dm:{self.dm.id}:msg:{root.id}", main_chunk_ids)
        self.assertNotIn(f"dm:{self.dm.id}:thread:{root.id}", by_id)

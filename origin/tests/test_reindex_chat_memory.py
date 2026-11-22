"""MIP-1868: the reindexer's chat stage stays inside its memory limit.

The production cron (500 MB) was killed with no traceback during chat
ingestion on every run, so its heartbeat never moved and the catch-up window
only grew. The fix changes HOW chat is loaded and written (one channel at a
time; embed + write + track in slices), never WHAT is indexed:

* `TestChatChunksArePinned` compares every chunk of a fixture channel with a
  golden file generated from the code before the change.
* `TestChatLoadedOneChannelAtATime` and `TestIngestionWritesInSlices` pin the
  bound; `TestKilledRunKeepsProgress` pins that a kill leaves what it wrote.

Regenerate the golden only for an intended change of the indexed text:
`WRITE_CHAT_CHUNK_GOLDEN=1 python manage.py test origin.tests.test_reindex_chat_memory`.
"""

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from unittest import mock

from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import ingestion
from origin.search_engine.chunkers import chat_chunker
from origin.search_engine.chunkers.base import Chunk, EntityChunks
from origin.search_engine.models import RagChunk, ThreadSummary
from origin.tests.test_base import BaseAPITestCase

GOLDEN = Path(__file__).parent / "fixtures" / "chat_chunks_pin.json"
GOLDEN_CASES = Path(__file__).parent / "fixtures" / "chat_chunks_pin_cases.json"
T0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _mid(n):
    return uuid.UUID(int=0xA0000 + n)


class _ChatFixture(BaseAPITestCase):
    def _channel(self, title, n_plain=0):
        ch = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title=title)
        ChannelMember.objects.create(channel=ch, user=self.user)
        ChannelMember.objects.create(channel=ch, user=self.user2)
        return ch

    def _msg(self, ch, n, text, sender=None, **extra):
        m = Message.objects.create(
            id=_mid(n),
            channel=ch,
            sender=sender or self.user,
            seq=n,
            body=_bn(text) if text is not None else [],
            **extra,
        )
        stamp = T0 + dt.timedelta(minutes=n)
        Message.objects.filter(pk=m.pk).update(ts_sent_at=stamp, ts_updated_at=stamp)
        return Message.objects.get(pk=m.pk)


def _sort_acl(entities):
    for e in entities:
        for c in e["chunks"]:
            c["acl_user_ids"] = sorted(c["acl_user_ids"])
    return entities


class TestChatChunksArePinned(_ChatFixture):
    def test_chunks_equal_the_golden(self):
        ch = self._channel("Pinned GM")
        self._msg(ch, 1, "first plain message")
        self._msg(ch, 2, "second plain message", sender=self.user2)
        root = self._msg(ch, 3, "thread root about the release")
        self._msg(
            ch, 4, "a reply", sender=self.user2, is_thread_reply=True, thread_root=root, parent=root
        )
        self._msg(ch, 5, "another reply", is_thread_reply=True, thread_root=root, parent=root)
        self._msg(ch, 6, None)  # empty text: no chunk
        self._msg(ch, 7, "third plain message")
        gone = self._msg(ch, 8, "deleted message")
        Message.objects.filter(pk=gone.pk).update(deleted_at=T0)
        self._msg(ch, 9, "fourth plain message", sender=self.user2)

        names = {
            str(self.team.team_id): "TEAM",
            str(self.user.id): "USER1",
            str(self.user2.id): "USER2",
            str(ch.id): "CHANNEL",
        }
        text = json.dumps(
            sorted(
                (
                    {"entity_id": b.entity_id, "chunks": [c.to_dict() for c in b.chunks]}
                    for b in chat_chunker.iter_gm_chunks()
                ),
                key=lambda e: e["entity_id"],
            ),
            indent=2,
            sort_keys=True,
        )
        for real, name in names.items():
            text = text.replace(real, name)
        text = text.replace(str(_mid(0))[:-5], "MSG-")  # message ids share this prefix
        if os.environ.get("WRITE_CHAT_CHUNK_GOLDEN"):
            GOLDEN.write_text(text + "\n")
        # Reader order follows random member uuids, so compare it sorted.
        self.assertEqual(_sort_acl(json.loads(text)), _sort_acl(json.loads(GOLDEN.read_text())))


class TestChatChunkCasesArePinned(_ChatFixture):
    """MIP-1914: the cases `TestChatChunksArePinned` leaves out, pinned the same way. The golden
    `chat_chunks_pin_cases.json` was generated from the chunker as it was BEFORE the MIP-1868
    change (origin/main ce3ca431), so it pins the old output, not the new one."""

    def test_chunks_equal_the_golden(self):
        # Task link: `Message.task_id` becomes `related_entity_ids` on the main timeline and on
        # the thread anchor and window.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Pin P", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(team=self.team, project=project, title="Pinned task")

        gm = self._channel("Linked GM")
        self._msg(gm, 1, "plain message linked to a task", task=task)
        self._msg(gm, 2, "plain message with no link")
        root = self._msg(gm, 3, "thread root linked to a task", task=task)
        self._msg(
            gm,
            4,
            "first reply",
            sender=self.user2,
            is_thread_reply=True,
            thread_root=root,
            parent=root,
        )
        # A task comment's mirror reply: out of the window and out of the next reply's context.
        self._msg(
            gm,
            5,
            "mirrored task comment text",
            is_thread_reply=True,
            thread_root=root,
            parent=root,
            task=task,
            metadata={"taskCommentId": 7},
        )
        self._msg(
            gm, 6, "reply after the mirror", is_thread_reply=True, thread_root=root, parent=root
        )
        # A thread with a ThreadSummary: no window chunk.
        sroot = self._msg(gm, 7, "summarised thread root")
        self._msg(
            gm,
            8,
            "summarised thread reply",
            sender=self.user2,
            is_thread_reply=True,
            thread_root=sroot,
            parent=sroot,
        )
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=2,
            chat_id=gm.id,
            thread_id=sroot.id,
            summary_text="summary",
        )
        # A soft-deleted thread root: anchor None, the replies still indexed.
        droot = self._msg(gm, 9, "deleted thread root")
        self._msg(
            gm,
            10,
            "reply under a deleted root",
            sender=self.user2,
            is_thread_reply=True,
            thread_root=droot,
            parent=droot,
        )
        Message.objects.filter(pk=droot.pk).update(deleted_at=T0)

        # A channel whose messages are all deleted: a tombstone entity with no chunks.
        dead = self._channel("All deleted GM")
        for n in (21, 22):
            m = self._msg(dead, n, f"deleted {n}")
            Message.objects.filter(pk=m.pk).update(deleted_at=T0)

        # A PM channel: title falls back to the project name, ACL from ProjectMembers.
        pm = Channel.objects.get(project=project, kind=ChannelKind.PM)
        self._msg(pm, 31, "project chat message")
        pm_root = self._msg(pm, 32, "project chat thread root")
        self._msg(
            pm,
            33,
            "project chat reply",
            sender=self.user2,
            is_thread_reply=True,
            thread_root=pm_root,
            parent=pm_root,
        )

        names = {
            str(self.team.team_id): "TEAM",
            str(self.user.id): "USER1",
            str(self.user2.id): "USER2",
            str(gm.id): "GM",
            str(dead.id): "DEAD",
            str(pm.id): "PM",
        }
        entities = [
            {"entity_id": b.entity_id, "chunks": [c.to_dict() for c in b.chunks]}
            for b in chat_chunker.iter_all_chat_chunks()
        ]
        for e in entities:
            for c in e["chunks"]:
                if c.get("project_id"):
                    c["project_id"] = "PROJECT"
                c["related_entity_ids"] = [
                    r.replace(f"task:{task.task_id}", "task:TASK") for r in c["related_entity_ids"]
                ]
        text = json.dumps(entities, indent=2, sort_keys=True)
        for real, name in names.items():
            text = text.replace(real, name)
        text = text.replace(str(_mid(0))[:-5], "MSG-")
        # Sorted after the real ids became names: the real ones are random uuids.
        text = json.dumps(
            sorted(json.loads(text), key=lambda e: e["entity_id"]), indent=2, sort_keys=True
        )
        if os.environ.get("WRITE_CHAT_CHUNK_GOLDEN"):
            GOLDEN_CASES.write_text(text + "\n")
        self.assertEqual(
            _sort_acl(json.loads(text)), _sort_acl(json.loads(GOLDEN_CASES.read_text()))
        )


class TestChatLoadedOneChannelAtATime(_ChatFixture):
    def test_the_next_channel_is_not_read_until_the_first_is_consumed(self):
        for i in range(3):
            ch = self._channel(f"GM {i}")
            self._msg(ch, 10 * (i + 1), f"hello {i}")
        real = chat_chunker._load_channel_messages
        with mock.patch.object(chat_chunker, "_load_channel_messages", side_effect=real) as load:
            stream = chat_chunker.iter_gm_chunks()
            next(stream)
            self.assertEqual(load.call_count, 1)
            list(stream)
            self.assertEqual(load.call_count, 3)
            self.assertEqual(len({c.args[0] for c in load.call_args_list}), 3)


def _entity(n, title="T"):
    chunks = [
        Chunk(
            chunk_id=f"chat:gm:c:msg:{i}",
            entity_type="chat",
            entity_id="gm:c",
            chunk_type="chat_message",
            team_id=str(uuid.UUID(int=1)),
            acl_user_ids=["u"],
            title=title,
            search_text=f"text {i}",
            snippet_text=f"text {i}",
        )
        for i in range(n)
    ]
    return EntityChunks(entity_type="chat", entity_id="gm:c", chunks=chunks)


class _Ingest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.embedded = []

        def embed(texts):
            self.embedded.append(len(texts))
            return [[0.1] * 8 for _ in texts]

        self.bulk_sizes = []

        def bulk(actions):
            self.bulk_sizes.append(len(actions))
            return ingestion.BulkResult()

        for p in (
            mock.patch.object(ingestion, "embed_texts", side_effect=embed),
            mock.patch.object(ingestion, "_bulk_index", side_effect=bulk),
            mock.patch.object(ingestion, "get_active_embedding_model_name", return_value="m"),
            mock.patch.object(ingestion, "get_index_alias", return_value="rag-test"),
            mock.patch.object(ingestion, "get_client"),
            mock.patch.object(ingestion.os_helpers, "bulk", return_value=(0, [])),
        ):
            p.start()
            self.addCleanup(p.stop)


@override_settings(
    SEARCH_ENGINE={
        **__import__("django.conf").conf.settings.SEARCH_ENGINE,
        "RAG_INGEST_SLICE_SIZE": 100,
    }
)
class TestIngestionWritesInSlices(_Ingest):
    def test_no_embed_call_or_bulk_holds_more_than_a_slice(self):
        stats = ingestion.IngestionStats()
        ingestion._ingest_entity(_entity(250), stats, dry_run=False)
        self.assertEqual(self.embedded, [100, 100, 50])
        self.assertEqual(self.bulk_sizes, [100, 100, 50])
        self.assertEqual(RagChunk.objects.filter(entity_id="gm:c").count(), 250)
        self.assertEqual(stats.chunks_new, 250)

    def test_a_second_pass_finds_everything_unchanged(self):
        ingestion._ingest_entity(_entity(250), ingestion.IngestionStats(), dry_run=False)
        self.embedded.clear()
        stats = ingestion.IngestionStats()
        ingestion._ingest_entity(_entity(250), stats, dry_run=False)
        self.assertEqual(self.embedded, [])
        self.assertEqual(stats.chunks_unchanged, 250)


@override_settings(
    SEARCH_ENGINE={
        **__import__("django.conf").conf.settings.SEARCH_ENGINE,
        "RAG_INGEST_SLICE_SIZE": 100,
    }
)
class TestKilledRunKeepsProgress(_Ingest):
    def test_slices_written_before_a_kill_are_not_redone(self):
        calls = {"n": 0}
        real = ingestion.embed_texts

        def die_on_third(texts):
            calls["n"] += 1
            if calls["n"] == 3:
                raise MemoryError("simulated kill")
            return real(texts)

        with mock.patch.object(ingestion, "embed_texts", side_effect=die_on_third):
            with self.assertRaises(MemoryError):
                ingestion._ingest_entity(_entity(250), ingestion.IngestionStats(), dry_run=False)
        self.assertEqual(RagChunk.objects.filter(entity_id="gm:c").count(), 200)

        self.embedded.clear()
        stats = ingestion.IngestionStats()
        ingestion._ingest_entity(_entity(250), stats, dry_run=False)
        self.assertEqual(self.embedded, [50])
        self.assertEqual(stats.chunks_unchanged, 200)
        self.assertEqual(stats.chunks_new, 50)
        self.assertEqual(RagChunk.objects.filter(entity_id="gm:c").count(), 250)


@override_settings(
    SEARCH_ENGINE={
        **__import__("django.conf").conf.settings.SEARCH_ENGINE,
        "RAG_INGEST_SLICE_SIZE": 100,
    }
)
class TestSliceWriteFailures(_Ingest):
    """MIP-1914: a write that fails in the MIDDLE slice, and the metadata-only lane in slices."""

    def _tracked(self):
        return set(RagChunk.objects.filter(entity_id="gm:c").values_list("chunk_id", flat=True))

    def _ids(self, rng):
        return {f"chat:gm:c:msg:{i}" for i in rng}

    def test_a_failed_middle_slice_is_not_tracked_and_is_the_only_one_redone(self):
        slice_two = self._ids(range(100, 200))
        calls = {"n": 0}

        def bulk(actions):
            calls["n"] += 1
            self.bulk_sizes.append(len(actions))
            if calls["n"] == 2:
                return ingestion.BulkResult(failed={a["_id"] for a in actions})
            return ingestion.BulkResult()

        stats = ingestion.IngestionStats()
        with mock.patch.object(ingestion, "_bulk_index", side_effect=bulk):
            ingestion._ingest_entity(_entity(250), stats, dry_run=False)
        # Slices 1 and 3 are tracked, slice 2 is not; the error names the count.
        self.assertEqual(self._tracked(), self._ids(range(0, 100)) | self._ids(range(200, 250)))
        self.assertEqual(self.embedded, [100, 100, 50])
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("bulk write failed for 100 chunk(s)", stats.errors[0])

        # The next pass re-embeds exactly the failed slice.
        self.embedded.clear()
        again = ingestion.IngestionStats()
        ingestion._ingest_entity(_entity(250), again, dry_run=False)
        self.assertEqual(self.embedded, [100])
        self.assertEqual(again.chunks_new, 100)
        self.assertEqual(again.chunks_unchanged, 150)
        self.assertEqual(
            self._tracked(), slice_two | self._ids(range(0, 100)) | self._ids(range(200, 250))
        )
        self.assertEqual(again.errors, [])

    def test_metadata_only_lane_writes_in_slices_and_drops_a_missing_doc(self):
        ingestion._ingest_entity(_entity(250), ingestion.IngestionStats(), dry_run=False)
        self.embedded.clear()
        self.bulk_sizes.clear()
        gone = "chat:gm:c:msg:150"  # in slice 2 of 3

        def bulk(actions):
            self.bulk_sizes.append(len(actions))
            return ingestion.BulkResult(missing={gone} & {a["_id"] for a in actions})

        stats = ingestion.IngestionStats()
        with mock.patch.object(ingestion, "_bulk_index", side_effect=bulk):
            ingestion._ingest_entity(_entity(250, title="T2"), stats, dry_run=False)
        # A new title is a metadata change: three update bulks, nothing re-embedded.
        self.assertEqual(stats.chunks_metadata_only, 250)
        self.assertEqual(self.bulk_sizes, [100, 100, 50])
        self.assertEqual(self.embedded, [])
        # The doc OpenSearch lacked loses its tracking row; every other chunk is tracked.
        self.assertEqual(self._tracked(), self._ids(range(250)) - {gone})
        self.assertEqual(stats.errors, [])

        # Next pass: that one chunk is new and re-embedded whole; the rest are unchanged.
        again = ingestion.IngestionStats()
        ingestion._ingest_entity(_entity(250, title="T2"), again, dry_run=False)
        self.assertEqual(self.embedded, [1])
        self.assertEqual(again.chunks_new, 1)
        self.assertEqual(again.chunks_unchanged, 249)

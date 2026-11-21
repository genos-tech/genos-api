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
from origin.search_engine import ingestion
from origin.search_engine.chunkers import chat_chunker
from origin.search_engine.chunkers.base import Chunk, EntityChunks
from origin.search_engine.models import RagChunk
from origin.tests.test_base import BaseAPITestCase

GOLDEN = Path(__file__).parent / "fixtures" / "chat_chunks_pin.json"
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


def _entity(n):
    chunks = [
        Chunk(
            chunk_id=f"chat:gm:c:msg:{i}",
            entity_type="chat",
            entity_id="gm:c",
            chunk_type="chat_message",
            team_id=str(uuid.UUID(int=1)),
            acl_user_ids=["u"],
            title="T",
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

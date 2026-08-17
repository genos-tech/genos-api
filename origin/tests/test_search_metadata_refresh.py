"""Tests for the ingestion pipeline's metadata-only refresh lane.

The bug these lock down: `_ingest_entity` used to hash ONLY
`search_text`, so a chunk whose text was untouched but whose metadata
moved — a task flipped Open → Closed, a reassignment, a milestone
detached, an ACL member removed — was classified "unchanged" and never
written. OpenSearch kept serving the old overlay indefinitely, which
surfaced as a Spotlight status chip that disagreed with the task and
that neither a page refresh nor a full reindex would correct.

OpenSearch is never touched: `_bulk_index` is patched and the actions it
would have shipped are asserted on directly. `embed_texts` is patched
too — the central claim of this lane is that it is NOT called.
"""

from unittest import mock

from django.test import TestCase

from origin.search_engine import ingestion
from origin.search_engine.chunkers.base import Chunk, EntityChunks
from origin.search_engine.models import RagChunk

TEAM_ID = "11111111-1111-1111-1111-111111111111"


def _chunk(**overrides) -> Chunk:
    kwargs = {
        "chunk_id": "task:1:title_content",
        "entity_type": "task",
        "entity_id": "task:1",
        "chunk_type": "task_title_content",
        "team_id": TEAM_ID,
        "title": "Fix the flaky test",
        "search_text": "Fix the flaky test\nIt fails on CI about once a run.",
        "snippet_text": "Fix the flaky test",
        "task_id": "1",
        "task_status": "WIP",
        "task_assignee_id": "7",
        "acl_user_ids": ["7", "9"],
        "updated_at": "2026-08-17T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return Chunk(**kwargs)


class MetaFingerprintTests(TestCase):
    """`_meta_fingerprint` must react to metadata and ignore text."""

    def test_status_change_moves_the_fingerprint(self):
        before = ingestion._meta_fingerprint(_chunk(task_status="WIP"))
        after = ingestion._meta_fingerprint(_chunk(task_status="Closed"))
        self.assertNotEqual(before, after)

    def test_clearing_a_field_moves_the_fingerprint(self):
        before = ingestion._meta_fingerprint(_chunk(task_assignee_id="7"))
        after = ingestion._meta_fingerprint(_chunk(task_assignee_id=None))
        self.assertNotEqual(before, after)

    def test_acl_change_moves_the_fingerprint(self):
        before = ingestion._meta_fingerprint(_chunk(acl_user_ids=["7", "9"]))
        after = ingestion._meta_fingerprint(_chunk(acl_user_ids=["7"]))
        self.assertNotEqual(before, after)

    def test_text_only_change_leaves_the_fingerprint_alone(self):
        # `text_hash` is what covers this; double-counting it here would
        # send pure content edits down the no-re-embed path.
        before = ingestion._meta_fingerprint(_chunk(search_text="one"))
        after = ingestion._meta_fingerprint(_chunk(search_text="two"))
        self.assertEqual(before, after)

    def test_fingerprint_is_stable_across_calls(self):
        self.assertEqual(
            ingestion._meta_fingerprint(_chunk()),
            ingestion._meta_fingerprint(_chunk()),
        )


class MetadataUpdateActionTests(TestCase):
    """`_to_os_metadata_update` must preserve the vector and clear nulls."""

    def test_is_a_partial_update_not_a_full_index(self):
        action = ingestion._to_os_metadata_update(_chunk())
        self.assertEqual(action["_op_type"], "update")
        self.assertEqual(action["_id"], "task:1:title_content")
        self.assertIn("doc", action)

    def test_never_touches_the_embedding_or_search_text(self):
        doc = ingestion._to_os_metadata_update(_chunk())["doc"]
        # Absent keys are left alone by a partial update — that is
        # precisely how the existing embedding survives.
        self.assertNotIn("embedding", doc)
        self.assertNotIn("embedding_model", doc)
        self.assertNotIn("search_text", doc)

    def test_carries_the_overlays(self):
        doc = ingestion._to_os_metadata_update(_chunk(task_status="Closed"))["doc"]
        self.assertEqual(doc["task_status"], "Closed")
        self.assertEqual(doc["acl_user_ids"], ["7", "9"])

    def test_writes_cleared_fields_as_explicit_nulls(self):
        # `Chunk.to_dict()` strips Nones; a partial update built from it
        # would leave a cleared assignee at its stale value forever.
        doc = ingestion._to_os_metadata_update(_chunk(task_assignee_id=None))["doc"]
        self.assertIn("task_assignee_id", doc)
        self.assertIsNone(doc["task_assignee_id"])


class IngestEntityClassificationTests(TestCase):
    """End-to-end bucket routing through `_ingest_entity`."""

    def setUp(self):
        super().setUp()
        self.stats = ingestion.IngestionStats()
        embed = mock.patch.object(
            ingestion, "embed_texts", side_effect=lambda texts: [[0.1] * 8 for _ in texts]
        )
        bulk = mock.patch.object(ingestion, "_bulk_index")
        model = mock.patch.object(
            ingestion, "get_active_embedding_model_name", return_value="test-embed-v1"
        )
        alias = mock.patch.object(ingestion, "get_index_alias", return_value="rag-test")
        self.embed = embed.start()
        self.bulk = bulk.start()
        model.start()
        alias.start()
        for p in (embed, bulk, model, alias):
            self.addCleanup(p.stop)

    def _ingest(self, chunk: Chunk):
        ingestion._ingest_entity(
            EntityChunks(entity_type="task", entity_id="task:1", chunks=[chunk]),
            self.stats,
            dry_run=False,
        )

    def _ops(self):
        """Flatten the bulk actions shipped across all calls."""
        return [action for call in self.bulk.call_args_list for action in call.args[0]]

    def test_first_pass_embeds_and_indexes(self):
        self._ingest(_chunk())
        self.assertEqual(self.stats.chunks_new, 1)
        self.assertEqual(self.stats.chunks_metadata_only, 0)
        self.embed.assert_called_once()
        ops = self._ops()
        self.assertEqual(len(ops), 1)
        # A full index op, not a partial update: it carries `_source`
        # with the freshly computed vector.
        self.assertNotIn("_op_type", ops[0])
        self.assertIn("embedding", ops[0]["_source"])
        row = RagChunk.objects.get(chunk_id="task:1:title_content")
        self.assertTrue(row.meta_hash)

    def test_status_only_change_refreshes_without_embedding(self):
        self._ingest(_chunk(task_status="WIP"))
        self.embed.reset_mock()
        self.bulk.reset_mock()

        self._ingest(_chunk(task_status="Closed"))

        self.assertEqual(self.stats.chunks_metadata_only, 1)
        self.assertEqual(self.stats.chunks_unchanged, 0)
        # The whole point: no embedding spend for a metadata edit.
        self.embed.assert_not_called()
        ops = self._ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["_op_type"], "update")
        self.assertEqual(ops[0]["doc"]["task_status"], "Closed")

    def test_metadata_refresh_is_not_repeated_next_pass(self):
        # `meta_hash` has to be persisted, or every subsequent pass
        # re-detects the same diff and re-issues the same write forever.
        self._ingest(_chunk(task_status="WIP"))
        self._ingest(_chunk(task_status="Closed"))
        self.bulk.reset_mock()
        self.stats = ingestion.IngestionStats()

        self._ingest(_chunk(task_status="Closed"))

        self.assertEqual(self.stats.chunks_unchanged, 1)
        self.assertEqual(self.stats.chunks_metadata_only, 0)
        self.assertEqual(self._ops(), [])

    def test_text_change_still_re_embeds(self):
        self._ingest(_chunk())
        self.embed.reset_mock()

        self._ingest(_chunk(search_text="Fix the flaky test\nNow with a root cause."))

        self.assertEqual(self.stats.chunks_changed, 1)
        self.assertEqual(self.stats.chunks_metadata_only, 0)
        self.embed.assert_called_once()

    def test_empty_meta_hash_row_self_heals(self):
        # Rows written before `meta_hash` existed default to "". The
        # first pass over each must rewrite its overlays — this is what
        # makes a one-off full reindex the repair tool for metadata that
        # already drifted, at zero embedding cost.
        self._ingest(_chunk(task_status="Closed"))
        RagChunk.objects.filter(chunk_id="task:1:title_content").update(meta_hash="")
        self.embed.reset_mock()
        self.bulk.reset_mock()
        self.stats = ingestion.IngestionStats()

        self._ingest(_chunk(task_status="Closed"))

        self.assertEqual(self.stats.chunks_metadata_only, 1)
        self.embed.assert_not_called()
        ops = self._ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["doc"]["task_status"], "Closed")

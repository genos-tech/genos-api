"""MIP-855: a failed OpenSearch write must not be recorded as indexed.

`_ingest_entity` used to write a `RagChunk` row for every chunk it
ATTEMPTED. After an OpenSearch redeploy mid-run, 709 chunks were missing a
field in the index while the tracker called them current, and a full
reindex (hash-gated) could not heal them. These tests pin the contract:

  * a chunk whose bulk write failed keeps its old row (or none), so the
    next pass retries exactly that chunk;
  * only transient failures (429, 5xx, no response) block tracking; a
    permanent 400 is logged at ERROR and tracked as before, or it would be
    re-embedded on every cron tick;
  * a metadata update for a document the index doesn't hold drops the row;
  * the delete paths mirror this: a failed delete keeps its row (a 404 is
    success);
  * `opensearch_reindex --repair` finds and clears the drift.

OpenSearch is never touched: `os_helpers.bulk`, `_bulk_index` and the
client are patched.
"""

import logging
import uuid
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from origin.search_engine import bulk_errors, ingestion, purge, repair
from origin.search_engine.chunkers.base import Chunk, EntityChunks
from origin.search_engine.ingestion import BulkResult
from origin.search_engine.management.commands import opensearch_reindex
from origin.search_engine.models import RagChunk
from origin.services import demo_seeder

TEAM_ID = "11111111-1111-1111-1111-111111111111"
A = "task:1:a"
B = "task:1:b"


def _chunk(cid, text="body", **overrides) -> Chunk:
    kwargs = {
        "chunk_id": cid,
        "entity_type": "task",
        "entity_id": "task:1",
        "chunk_type": "task_comment",
        "team_id": TEAM_ID,
        "title": "T",
        "search_text": text,
        "snippet_text": text,
        "task_id": "1",
        "task_status": "WIP",
        "acl_user_ids": ["7"],
        "updated_at": "2026-08-17T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return Chunk(**kwargs)


def _row(cid, **overrides):
    kwargs = dict(
        chunk_id=cid,
        entity_type="task",
        entity_id="task:1",
        chunk_type="task_comment",
        team_id=TEAM_ID,
        text_hash="h",
        meta_hash="m",
        embedding_model="test-embed-v1",
        index_schema_version="v3",
    )
    kwargs.update(overrides)
    return RagChunk.objects.create(**kwargs)


def _entry(op, cid, status, etype=None, *, error=None):
    body = {"_id": cid, "status": status}
    if etype:
        body["error"] = {"type": etype}
    elif error is not None:
        body["error"] = error
    return {op: body}


class IngestTrackingTests(TestCase):
    """End to end through `_ingest_entity` with `_bulk_index` reporting."""

    def setUp(self):
        super().setUp()
        self.stats = ingestion.IngestionStats()
        self.embed = mock.patch.object(
            ingestion, "embed_texts", side_effect=lambda texts: [[0.1] * 8 for _ in texts]
        ).start()
        self.bulk = mock.patch.object(ingestion, "_bulk_index").start()
        self.bulk.return_value = BulkResult()
        mock.patch.object(
            ingestion, "get_active_embedding_model_name", return_value="test-embed-v1"
        ).start()
        mock.patch.object(ingestion, "get_index_alias", return_value="rag-test").start()
        mock.patch.object(ingestion, "get_client").start()
        mock.patch.object(ingestion.os_helpers, "bulk", return_value=(0, [])).start()
        self.addCleanup(mock.patch.stopall)

    def _ingest(self, *chunks, stats=None):
        ingestion._ingest_entity(
            EntityChunks(entity_type="task", entity_id="task:1", chunks=list(chunks)),
            stats or self.stats,
            dry_run=False,
        )

    def _shipped_ids(self, call):
        return {a["_id"] for a in call.args[0]}

    def test_failed_write_is_not_tracked_and_next_pass_retries_only_it(self):
        """The acceptance test: patch `_bulk_index` to fail one id."""
        self.bulk.return_value = BulkResult(failed={B})
        self._ingest(_chunk(A, "alpha"), _chunk(B, "beta"))

        self.assertTrue(RagChunk.objects.filter(chunk_id=A).exists())
        self.assertFalse(
            RagChunk.objects.filter(chunk_id=B).exists(),
            "a chunk whose write failed must not get a tracking row",
        )
        self.assertEqual(len(self.stats.errors), 1)
        self.assertIn("task/task:1", self.stats.errors[0])

        # Second pass, OpenSearch healthy again.
        self.bulk.reset_mock()
        self.bulk.return_value = BulkResult()
        self.embed.reset_mock()
        stats2 = ingestion.IngestionStats()
        self._ingest(_chunk(A, "alpha"), _chunk(B, "beta"), stats=stats2)

        self.assertEqual((stats2.chunks_new, stats2.chunks_unchanged), (1, 1))
        self.assertEqual(self.embed.call_args.args[0], ["beta"])
        self.assertEqual(self._shipped_ids(self.bulk.call_args), {B})
        self.assertTrue(RagChunk.objects.filter(chunk_id=B).exists())
        self.assertEqual(stats2.errors, [])

    def test_failed_text_change_keeps_the_old_hash_so_it_stays_changed(self):
        self._ingest(_chunk(A, "old"))
        old_hash = RagChunk.objects.get(chunk_id=A).text_hash

        self.bulk.return_value = BulkResult(failed={A})
        self._ingest(_chunk(A, "new"))
        self.assertEqual(RagChunk.objects.get(chunk_id=A).text_hash, old_hash)

        self.bulk.return_value = BulkResult()
        stats2 = ingestion.IngestionStats()
        self._ingest(_chunk(A, "new"), stats=stats2)
        self.assertEqual(stats2.chunks_changed, 1)
        self.assertNotEqual(RagChunk.objects.get(chunk_id=A).text_hash, old_hash)

    def test_failed_metadata_update_stays_metadata_only(self):
        self._ingest(_chunk(A, task_status="WIP"))
        old_meta = RagChunk.objects.get(chunk_id=A).meta_hash

        self.bulk.return_value = BulkResult(failed={A})
        self._ingest(_chunk(A, task_status="Closed"))
        self.assertEqual(RagChunk.objects.get(chunk_id=A).meta_hash, old_meta)

        self.bulk.return_value = BulkResult()
        self.embed.reset_mock()
        stats2 = ingestion.IngestionStats()
        self._ingest(_chunk(A, task_status="Closed"), stats=stats2)
        self.assertEqual(stats2.chunks_metadata_only, 1)
        self.embed.assert_not_called()
        self.assertNotEqual(RagChunk.objects.get(chunk_id=A).meta_hash, old_meta)

    def test_metadata_update_on_a_missing_document_drops_the_row_then_reembeds(self):
        self._ingest(_chunk(A, task_status="WIP"))
        self.bulk.return_value = BulkResult(missing={A})
        self._ingest(_chunk(A, task_status="Closed"))
        self.assertFalse(RagChunk.objects.filter(chunk_id=A).exists())
        self.assertEqual(self.stats.errors, [], "self-healing, not a failed run")

        self.bulk.return_value = BulkResult()
        self.embed.reset_mock()
        stats2 = ingestion.IngestionStats()
        self._ingest(_chunk(A, task_status="Closed"), stats=stats2)
        self.assertEqual(stats2.chunks_new, 1)
        self.embed.assert_called_once()
        self.assertTrue(RagChunk.objects.filter(chunk_id=A).exists())

    def test_a_bulk_failure_keeps_the_acl_marks(self):
        """`stats.errors` was empty on a bulk failure, so `_may_release`
        let go of marks whose ACL update had not landed."""
        dirty = mock.Mock(rows=[object()])
        self.bulk.return_value = BulkResult(failed={A})
        self._ingest(_chunk(A))
        self.assertFalse(opensearch_reindex._may_release(dirty, self.stats, {"entity_types": None}))

    def test_conversation_hook_reports_a_failed_write(self):
        entity = EntityChunks(entity_type="task", entity_id="task:1", chunks=[_chunk(A)])
        self.bulk.return_value = BulkResult(failed={A})
        with mock.patch.object(ingestion, "conversation_chunks_for_run", return_value=entity):
            self.assertFalse(ingestion.ingest_conversation_run(object()))
        self.assertFalse(RagChunk.objects.filter(chunk_id=A).exists())


class BulkIndexClassificationTests(TestCase):
    """`_bulk_index` sorts error entries into failed / missing / permanent."""

    def setUp(self):
        super().setUp()
        mock.patch.object(ingestion, "get_index_alias", return_value="rag-test").start()
        mock.patch.object(ingestion, "get_client").start()
        mock.patch.object(ingestion, "ensure_additive_mapping").start()
        self.bulk = mock.patch.object(ingestion.os_helpers, "bulk").start()
        self.addCleanup(mock.patch.stopall)

    def _run(self, errors, actions=None, level="WARNING"):
        self.bulk.return_value = (0, errors)
        actions = actions or [
            {"_op_type": "update", "_id": A},
            {"_op_type": "update", "_id": B},
        ]
        with self.assertLogs(ingestion.log, level=level) as cm:
            # assertLogs needs one record; the happy path logs none.
            ingestion.log.warning("sentinel")
            result = ingestion._bulk_index(actions)
        return result, cm

    def test_transient_statuses_are_failed(self):
        for status in (429, 500, 502, 503, 408, "N/A", None):
            result, _ = self._run([_entry("index", A, status, "boom")])
            self.assertEqual(result.failed, {A}, status)

    def test_transport_failure_entry_is_failed(self):
        """What `_process_bulk_chunk_error` yields: `error` is a string."""
        entry = {"index": {"_id": A, "status": "N/A", "error": "ConnectionError", "data": {}}}
        result, _ = self._run([entry])
        self.assertEqual(result.failed, {A})

    def test_permanent_400_is_logged_once_at_error_and_not_failed(self):
        result, cm = self._run([_entry("index", A, 400, "mapper_parsing_exception")])
        self.assertEqual(result.failed, set(), "a retry would fail identically, forever")
        self.assertEqual(len([r for r in cm.records if r.levelno >= logging.ERROR]), 1)

    def test_version_conflict_is_benign(self):
        result, cm = self._run([_entry("update", A, 409, "version_conflict_engine_exception")])
        self.assertEqual(result.failed, set())
        self.assertEqual(result.missing, set())
        self.assertFalse([r for r in cm.records if r.levelno >= logging.ERROR])

    def test_update_on_a_missing_document_is_missing_not_failed_and_not_red(self):
        result, cm = self._run([_entry("update", A, 404, "document_missing_exception")])
        self.assertEqual(result.missing, {A})
        self.assertEqual(result.failed, set())
        self.assertFalse([r for r in cm.records if r.levelno >= logging.ERROR])

    def test_an_entry_without_an_id_fails_the_whole_batch(self):
        result, _ = self._run([{"index": {"status": 503, "error": {"type": "x"}}}])
        self.assertEqual(result.failed, {A, B})

    def test_mixed_batch_attributes_each_entry(self):
        result, cm = self._run(
            [
                _entry("update", A, 503, "x"),
                _entry("update", B, 400, "mapper_parsing_exception"),
            ]
        )
        self.assertEqual(result.failed, {A})
        self.assertEqual(len([r for r in cm.records if r.levelno >= logging.ERROR]), 1)

    def test_cluster_block_is_transient_and_other_403_is_not(self):
        self.assertTrue(bulk_errors.is_transient(403, "cluster_block_exception"))
        self.assertFalse(bulk_errors.is_transient(403, "security_exception"))

    def test_no_actions_is_an_empty_result(self):
        self.assertEqual(ingestion._bulk_index([]), BulkResult())
        self.bulk.assert_not_called()


class DeleteFailureTests(TestCase):
    """A failed delete keeps the tracking row; a 404 is success."""

    def setUp(self):
        super().setUp()
        mock.patch.object(ingestion, "get_index_alias", return_value="rag-test").start()
        mock.patch.object(ingestion, "get_client").start()
        # `ingestion.os_helpers` and `purge.os_helpers` are the same module,
        # so one patch serves both.
        self.bulk = self.pbulk = mock.patch.object(ingestion.os_helpers, "bulk").start()
        mock.patch.object(purge, "get_index_alias", return_value="rag-test").start()
        mock.patch.object(purge, "get_client").start()
        self.addCleanup(mock.patch.stopall)
        _row(A)
        _row(B)

    def _stale(self, errors):
        self.bulk.return_value = (0, errors)
        stats = ingestion.IngestionStats()
        ingestion._delete_stale("task", "task:1", keep_chunk_ids=set(), stats=stats)
        return stats

    def _alive(self):
        return set(RagChunk.objects.values_list("chunk_id", flat=True))

    def test_stale_delete_failure_keeps_only_that_row(self):
        stats = self._stale([_entry("delete", B, 503, "x")])
        self.assertEqual(self._alive(), {B})
        self.assertEqual(stats.chunks_deleted, 1)
        self.assertEqual(len(stats.errors), 1)

    def test_stale_delete_404_removes_the_row(self):
        stats = self._stale([_entry("delete", B, 404, "x")])
        self.assertEqual(self._alive(), set())
        self.assertEqual(stats.errors, [])

    def test_stale_delete_failure_is_retried_next_pass(self):
        self._stale([_entry("delete", B, 500, "x")])
        stats = self._stale([])
        self.assertEqual(self._alive(), set())
        self.assertEqual(stats.chunks_deleted, 1)

    def test_stale_delete_entry_without_an_id_keeps_every_row(self):
        self._stale([{"delete": {"status": "N/A", "error": "ConnectionError"}}])
        self.assertEqual(self._alive(), {A, B})

    def test_purge_chunks_keeps_rows_of_failed_deletes(self):
        self.pbulk.return_value = (1, [_entry("delete", A, 429, "x")])
        removed = purge.purge_chunks([A, B])
        self.assertEqual(removed, 1)
        self.assertEqual(self._alive(), {A})

    def test_purge_chunks_404_is_success(self):
        self.pbulk.return_value = (0, [_entry("delete", A, 404, "x")])
        self.assertEqual(purge.purge_chunks([A, B]), 2)
        self.assertEqual(self._alive(), set())

    def test_purge_chunks_transport_failure_keeps_all_rows(self):
        entries = [
            {"delete": {"_id": A, "status": "N/A", "error": "boom"}},
            {"delete": {"_id": B, "status": "N/A", "error": "boom"}},
        ]
        self.pbulk.return_value = (0, entries)
        self.assertEqual(purge.purge_chunks([A, B]), 0)
        self.assertEqual(self._alive(), {A, B})

    def test_demo_cleanup_keeps_rows_of_failed_deletes(self):
        with (
            mock.patch(
                "opensearchpy.helpers.bulk", return_value=(1, [_entry("delete", A, 503, "x")])
            ),
            mock.patch("origin.search_engine.opensearch_client.get_client"),
            mock.patch("origin.search_engine.opensearch_client.get_index_alias", return_value="x"),
        ):
            demo_seeder._delete_demo_team_search_chunks(uuid.UUID(TEAM_ID))
        self.assertEqual(self._alive(), {A})

    def test_demo_cleanup_keeps_every_row_when_opensearch_is_down(self):
        with (
            mock.patch("opensearchpy.helpers.bulk", side_effect=ConnectionError("down")),
            mock.patch("origin.search_engine.opensearch_client.get_client"),
            mock.patch("origin.search_engine.opensearch_client.get_index_alias", return_value="x"),
        ):
            demo_seeder._delete_demo_team_search_chunks(uuid.UUID(TEAM_ID))
        self.assertEqual(self._alive(), {A, B})

    def test_demo_cleanup_drops_everything_when_all_deletes_land(self):
        with (
            mock.patch(
                "opensearchpy.helpers.bulk", return_value=(2, [_entry("delete", B, 404, "x")])
            ),
            mock.patch("origin.search_engine.opensearch_client.get_client"),
            mock.patch("origin.search_engine.opensearch_client.get_index_alias", return_value="x"),
        ):
            demo_seeder._delete_demo_team_search_chunks(uuid.UUID(TEAM_ID))
        self.assertEqual(self._alive(), set())


class RepairTests(TestCase):
    """`repair.find_drift` / `apply` and the `--repair` command."""

    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()
        mock.patch.object(repair, "get_client", return_value=self.client).start()
        mock.patch.object(repair, "get_index_alias", return_value="rag-test").start()
        self.scan = mock.patch.object(repair.os_helpers, "scan", return_value=iter([])).start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _doc(cid, found=True, **src):
        base = {
            "text_hash": "h",
            "embedding_model": "test-embed-v1",
            "index_schema_version": "v3",
        }
        base.update(src)
        d = {"_id": cid, "found": found}
        if found:
            d["_source"] = base
        return d

    def _mget(self, docs):
        self.client.mget.return_value = {"docs": docs}

    def test_classifies_ok_missing_stale_and_unverifiable(self):
        for cid in ("ok", "gone", "oldtext", "oldmodel", "legacy"):
            _row(cid)
        self._mget(
            [
                self._doc("ok"),
                self._doc("gone", found=False),
                self._doc("oldtext", text_hash="other"),
                self._doc("oldmodel", embedding_model="old-model"),
                {"_id": "legacy", "found": True, "_source": {"text_hash": "h"}},
            ]
        )
        drift = repair.find_drift(check_untracked=False)
        self.assertEqual(drift.missing_ids, ["gone"])
        self.assertEqual(sorted(drift.mismatch_ids), ["oldmodel", "oldtext"])
        self.assertEqual(drift.unverifiable, 1)
        self.assertEqual(drift.scanned, 5)
        self.assertEqual(drift.entity_types, {"task": 3})

    def test_uses_mget_not_search(self):
        _row("ok")
        self._mget([self._doc("ok")])
        repair.find_drift(check_untracked=False)
        self.client.mget.assert_called_once()
        self.client.search.assert_not_called()
        self.assertEqual(self.client.mget.call_args.kwargs["body"], {"ids": ["ok"]})

    def test_an_unreadable_item_is_an_error_not_a_missing_doc(self):
        _row("ok")
        self._mget([{"_id": "ok", "error": {"type": "shard_failure"}}])
        with self.assertRaises(RuntimeError):
            repair.find_drift(check_untracked=False)

    def test_entity_type_scope(self):
        _row("t1")
        _row("c1", entity_type="chat", entity_id="chat:1")
        self._mget([self._doc("c1")])
        drift = repair.find_drift(["chat"], check_untracked=False)
        self.assertEqual(drift.scanned, 1)
        self.assertEqual(self.client.mget.call_args.kwargs["body"], {"ids": ["c1"]})

    def test_reports_untracked_documents_without_acting_on_them(self):
        _row("ok")
        self._mget([self._doc("ok")])
        self.scan.return_value = iter([{"_id": "ok"}, {"_id": "stray"}])
        drift = repair.find_drift()
        self.assertEqual(drift.untracked_ids, ["stray"])
        self.assertEqual(drift.repairable_ids, [])
        self.assertEqual(repair.apply(drift), 0)

    def test_apply_drops_exactly_the_missing_and_stale_rows(self):
        for cid in ("ok", "gone", "oldtext"):
            _row(cid)
        self._mget(
            [self._doc("ok"), self._doc("gone", found=False), self._doc("oldtext", text_hash="x")]
        )
        drift = repair.find_drift(check_untracked=False)
        self.assertEqual(repair.apply(drift), 2)
        self.assertEqual(list(RagChunk.objects.values_list("chunk_id", flat=True)), ["ok"])

    def test_blank_meta_hashes_is_scoped(self):
        _row("t1")
        _row("c1", entity_type="chat", entity_id="chat:1")
        self.assertEqual(repair.blank_meta_hashes(["chat"]), 1)
        self.assertEqual(RagChunk.objects.get(chunk_id="c1").meta_hash, "")
        self.assertEqual(RagChunk.objects.get(chunk_id="t1").meta_hash, "m")

    # -- the command -----------------------------------------------------

    def _call(self, *args):
        out = StringIO()
        with (
            mock.patch.object(opensearch_reindex, "get_client"),
            mock.patch.object(opensearch_reindex, "sweep_orphans", return_value={}),
            mock.patch.object(opensearch_reindex.Command, "_warn_on_schema_mismatch"),
            mock.patch.object(opensearch_reindex, "ingest_all") as ingest,
        ):
            ingest.return_value = ingestion.IngestionStats()
            call_command("opensearch_reindex", *args, stdout=out)
        return ingest, out.getvalue()

    def test_repair_is_a_dry_run_by_default(self):
        _row("gone")
        self._mget([self._doc("gone", found=False)])
        ingest, out = self._call("--repair")
        self.assertTrue(RagChunk.objects.filter(chunk_id="gone").exists())
        ingest.assert_not_called()
        self.assertIn('"tracked_missing_in_index": 1', out)
        self.assertIn("dry run", out)

    def test_repair_apply_drops_rows_and_reingests_only_the_drifted_types(self):
        _row("gone")
        _row("fine", entity_type="chat", entity_id="chat:1")
        self._mget([self._doc("gone", found=False), self._doc("fine")])
        ingest, _ = self._call("--repair", "--apply")
        self.assertFalse(RagChunk.objects.filter(chunk_id="gone").exists())
        self.assertTrue(RagChunk.objects.filter(chunk_id="fine").exists())
        kwargs = ingest.call_args.kwargs
        self.assertEqual(kwargs["entity_types"], ["task"])
        self.assertIsNone(kwargs["since"], "a full pass, or an old entity is never re-emitted")

    def test_repair_apply_with_no_drift_does_nothing(self):
        _row("fine")
        self._mget([self._doc("fine")])
        ingest, out = self._call("--repair", "--apply")
        ingest.assert_not_called()
        self.assertIn("Nothing to repair", out)

    def test_repair_metadata_blanks_hashes_and_covers_the_scope(self):
        _row("fine")
        self._mget([self._doc("fine")])
        ingest, _ = self._call("--repair", "--apply", "--repair-metadata", "--entity-types", "task")
        self.assertEqual(RagChunk.objects.get(chunk_id="fine").meta_hash, "")
        self.assertEqual(ingest.call_args.kwargs["entity_types"], ["task"])

    def test_apply_without_repair_is_refused(self):
        with self.assertRaises(CommandError):
            self._call("--apply")
        with self.assertRaises(CommandError):
            self._call("--repair", "--repair-metadata")

"""C1 conversation-memory freshness (§4.7) — window fix + completion hook.

Covers the two halves of near-real-time conversation indexing:

  * `conversation_chunker` — the incremental window must select runs by
    COMPLETION time (`finished_at`), not start time: the reindex cron
    passes `--since-minutes 11` on a 10-minute schedule, so a run that
    started before the window but finished inside it (long tool loop, or
    an `awaiting_approval` run resumed later) used to slip past every
    incremental pass, permanently. Legacy rows with a null `finished_at`
    fall back to `started_at`.
  * `conversation_chunks_for_run` — the shared single-run builder the
    hook and the batch iterator both use (status/emptiness gates, ACL).
  * `ingestion.ingest_conversation_run` — one embed + bulk write +
    explicit refresh (deferred-refresh default would otherwise leave the
    chunk invisible until the next cron pass), RagChunk tracking row, and
    the non-indexable early-out.

Embeddings and OpenSearch are mocked — these tests assert orchestration,
not vector math.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from origin.search_engine.chunkers.conversation_chunker import (
    conversation_chunks_for_run,
    iter_conversation_chunks,
)
from origin.search_engine.ingestion import ingest_conversation_run
from origin.search_engine.models import AgentRun, RagChunk

_TEAM = "11111111-1111-4111-8111-111111111111"
_USER = "22222222-2222-4222-8222-222222222222"


def _mk_run(
    *,
    status: str = "done",
    query: str = "what is the perf budget?",
    answer: str = "Lighthouse >= 95, bundle <= 120KB.",
    started_delta_min: int = 0,
    finished_delta_min: int | None = 0,
) -> AgentRun:
    """Create a run and force its timestamps (auto_now_add ignores kwargs,
    so started_at is set via a queryset update)."""
    run = AgentRun.objects.create(
        team_id=_TEAM,
        user_id=_USER,
        query=query,
        status=status,
        final_answer_text=answer,
    )
    now = timezone.now()
    AgentRun.objects.filter(pk=run.pk).update(
        started_at=now - timedelta(minutes=started_delta_min),
        finished_at=(
            now - timedelta(minutes=finished_delta_min)
            if finished_delta_min is not None
            else None
        ),
    )
    run.refresh_from_db()
    return run


class TestIncrementalWindowSemantics(TestCase):
    def test_run_started_before_window_but_finished_inside_is_yielded(self):
        # The bug this guards: an 11-minute window used to miss this run
        # forever because the filter keyed on started_at.
        _mk_run(started_delta_min=45, finished_delta_min=2)
        since = timezone.now() - timedelta(minutes=11)
        ids = [e.entity_id for e in iter_conversation_chunks(since=since)]
        self.assertEqual(len(ids), 1)

    def test_run_finished_before_window_is_skipped(self):
        _mk_run(started_delta_min=45, finished_delta_min=30)
        since = timezone.now() - timedelta(minutes=11)
        self.assertEqual(list(iter_conversation_chunks(since=since)), [])

    def test_legacy_null_finished_at_falls_back_to_started_at(self):
        _mk_run(started_delta_min=2, finished_delta_min=None)  # inside window
        _mk_run(
            query="older", started_delta_min=45, finished_delta_min=None
        )  # outside window
        since = timezone.now() - timedelta(minutes=11)
        batches = list(iter_conversation_chunks(since=since))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].chunks[0].title, "what is the perf budget?")

    def test_no_since_yields_everything_indexable(self):
        _mk_run(started_delta_min=45, finished_delta_min=30)
        _mk_run(query="second", started_delta_min=1, finished_delta_min=0)
        self.assertEqual(len(list(iter_conversation_chunks())), 2)


class TestConversationChunksForRun(TestCase):
    def test_done_run_builds_single_per_user_chunk(self):
        run = _mk_run()
        entity = conversation_chunks_for_run(run)
        self.assertIsNotNone(entity)
        self.assertEqual(entity.entity_id, f"conversation:{run.run_id}")
        self.assertEqual(len(entity.chunks), 1)
        chunk = entity.chunks[0]
        self.assertEqual(chunk.acl_user_ids, [_USER])
        self.assertIn("Q: what is the perf budget?", chunk.search_text)
        self.assertIn("A: Lighthouse", chunk.search_text)

    def test_non_done_statuses_are_not_memory(self):
        for status in ("error", "step_cap", "rejected", "awaiting_approval", "running"):
            run = _mk_run(status=status)
            self.assertIsNone(conversation_chunks_for_run(run), status)

    def test_empty_answer_or_query_is_skipped(self):
        self.assertIsNone(conversation_chunks_for_run(_mk_run(answer="")))
        self.assertIsNone(conversation_chunks_for_run(_mk_run(query="   ")))


class TestIngestConversationRun(TestCase):
    def _run_with_mocks(self, run):
        fake_client = MagicMock()
        with (
            patch(
                "origin.search_engine.ingestion.embed_texts",
                return_value=[[0.1, 0.2, 0.3]],
            ) as embed,
            patch("origin.search_engine.ingestion.get_client", return_value=fake_client),
            patch("origin.search_engine.ingestion.os_helpers") as helpers,
        ):
            helpers.bulk.return_value = (1, [])
            indexed = ingest_conversation_run(run)
        return indexed, embed, fake_client, helpers

    def test_done_run_is_embedded_indexed_and_refreshed(self):
        run = _mk_run()
        indexed, embed, client, helpers = self._run_with_mocks(run)
        self.assertTrue(indexed)
        embed.assert_called_once()
        helpers.bulk.assert_called()
        # The explicit refresh is the point of the hook: deferred-refresh
        # mode would otherwise hide the chunk until the next cron pass.
        client.indices.refresh.assert_called()
        # Tracking row mirrors the write, so the later cron pass is a
        # hash-diff no-op instead of a re-embed.
        self.assertTrue(
            RagChunk.objects.filter(chunk_id=f"conversation:{run.run_id}").exists()
        )

    def test_second_ingest_is_a_noop_on_unchanged_content(self):
        run = _mk_run()
        self._run_with_mocks(run)
        indexed, embed, _client, helpers = self._run_with_mocks(run)
        self.assertTrue(indexed)
        embed.assert_not_called()
        helpers.bulk.assert_not_called()

    def test_non_indexable_run_returns_false_without_side_effects(self):
        run = _mk_run(status="error")
        indexed, embed, client, helpers = self._run_with_mocks(run)
        self.assertFalse(indexed)
        embed.assert_not_called()
        helpers.bulk.assert_not_called()
        client.indices.refresh.assert_not_called()
        self.assertFalse(RagChunk.objects.filter(entity_id__contains=str(run.run_id)).exists())

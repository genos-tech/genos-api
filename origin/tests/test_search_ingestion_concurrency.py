"""Concurrency contracts for the ingestion pipeline's RagChunk writes.

Two overlapping reindexes used to deadlock in Postgres:

    DeadlockDetected: deadlock detected
    CONTEXT: while locking tuple (52,21) in relation "search_engine_ragchunk"

reported as `Failed to ingest entity <type>/<id>` plus `CommandError: Run
logged N error(s)` — which reads like a per-entity data defect, though the
named entity is fine and reindexes cleanly on its own.

**Why it happened.** `_upsert_tracking` mirrors chunks with
`update_or_create`, which Django implements as `SELECT ... FOR UPDATE`
followed by `UPDATE` (verified by capturing the SQL, not inferred). Because
the function's own `@transaction.atomic` is the outer transaction, Django's
inner one degrades to a savepoint, so every row lock is held until the whole
loop commits. The ids arrived as a `set`, and `str` hashing is salted per
process, so two processes locked the same rows in different orders — N held
locks, arbitrary order, two writers.

**Why it is reachable in production, not just under a stress test.** The
Railway cron runs every 10 minutes with `--since-minutes 11`, a deliberate
overlap; `kick_off_demo_reindex` fires a full reindex on a daemon thread
after every demo signin, team join and starter-workspace seed; and
`ingest_conversation_run` re-enters the same code once per completed ask.

The fix is deterministic lock ordering (`sorted`), so the tests below assert
ORDER — that is the whole contract. They are single-threaded on purpose,
following `test_sprint_bootstrap_race.py`: `TestCase` wraps each test in a
transaction its threads cannot see, and `TransactionTestCase` plus real
threads against the test DB is flaky by nature. A sorted list is a total
order, so asserting it once is as strong as asserting it under contention —
and `_HostileSet` stands in for the differently-salted second process,
because a single interpreter cannot otherwise produce the disagreement.

Measured against the live dev database (1448 chunks, two concurrent full
reindexes, every `meta_hash` cleared to force maximum contention), reverting
the `sorted` alone and changing nothing else:

    with `sorted`:     exit 0 / 0,  0 deadlock lines      (3 of 3 rounds)
    without `sorted`:  exit 1 / 1,  119-134 deadlock lines (3 of 3 rounds)

**Scope note, because the obvious extra fix is a no-op.** The two delete
paths over the same table (`_delete_stale`, `purge.purge_chunks`) look like
they need the same treatment and do not: `EXPLAIN` shows each is one
statement planned as a bitmap/seq scan, so Postgres locks in *physical*
order regardless of the `IN`-list order, and both run in autocommit (every
`purge_*` caller is deliberately placed after its transaction commits), so
locks release at statement end. Only a *held* lock can join a cycle.
Sorting them was tried and measured: 0 deadlocks either way, and 300-row
opposite-order concurrent deletes produced 0 deadlocks in 25 rounds. So
there is nothing here to pin for them — which is why this file tests one
ordering contract rather than three.

Removing the deadlock then exposed a second, pre-existing failure that the
aborted runs had been hiding: OpenSearch answers a concurrent update to the
same document with a 409 `version_conflict_engine_exception`. That is a
success in disguise — the other writer's value is at least as fresh as ours
— but it was logged at ERROR, so the `CronCommand` tripwire redded a run
whose work had all landed. Same for a 404 on the stale-chunk delete: the doc
is gone, which is the outcome the delete wanted. Both are now discounted,
which is what `purge.py` already did for its own 404s.
"""

import logging
from unittest import mock

from django.test import TestCase

from origin.search_engine import ingestion
from origin.search_engine.chunkers.base import Chunk
from origin.search_engine.models import RagChunk

TEAM_ID = "22222222-2222-2222-2222-222222222222"


def _chunk(cid: str, **overrides) -> Chunk:
    kwargs = {
        "chunk_id": cid,
        "entity_type": "task",
        "entity_id": "task:1",
        "chunk_type": "task_title_content",
        "team_id": TEAM_ID,
        "title": "Fix the flaky test",
        "search_text": f"body of {cid}",
        "snippet_text": "Fix the flaky test",
        "task_id": "1",
    }
    kwargs.update(overrides)
    return Chunk(**kwargs)


class _HostileSet(set):
    """A `set` that iterates in the worst possible order.

    In production the hazard is that `affected_ids` iterates *differently
    in another process* — `str` hashing is salted per process. A
    single-process test cannot reproduce that: within one interpreter the
    same ids iterate the same way every time, which is exactly why the
    obvious "build the set two ways and compare" test passes even with the
    bug present (verified).

    So the second process is stood in for explicitly. This yields the
    reverse of the required lock order, which pins the property that
    actually matters: the write order is a function of the id *set*, never
    of the order that set happens to hand out.
    """

    def __iter__(self):
        return iter(sorted(super().__iter__(), reverse=True))


class LockOrderTests(TestCase):
    """`_upsert_tracking` must take its row locks in a fixed global order."""

    def _upsert(self, ids, container=set):
        """Drive the real `_upsert_tracking`, recording write order.

        `update_or_create` is wrapped rather than the SQL captured: it is
        the call that emits `SELECT ... FOR UPDATE`, so its call order IS
        the lock order. The real manager method still runs, so the rows
        land and the assertions below can read them back.
        """
        chunks = {cid: _chunk(cid) for cid in ids}
        seen: list[str] = []
        real = RagChunk.objects.update_or_create

        def recording(*args, **kwargs):
            seen.append(kwargs["chunk_id"])
            return real(*args, **kwargs)

        with mock.patch.object(RagChunk.objects, "update_or_create", recording):
            ingestion._upsert_tracking(
                entity_type="task",
                entity_id="task:1",
                chunks=chunks,
                hashes={cid: f"h-{cid}" for cid in ids},
                meta_hashes={cid: f"m-{cid}" for cid in ids},
                model="test-embed-v1",
                affected_ids=container(ids),
            )
        return seen

    def test_rows_are_locked_in_sorted_order(self):
        # Chosen so sorted order differs from insertion order, and so
        # lexicographic order differs from numeric (`comment:10` sorts
        # before `comment:2`) — the assertion has to describe what the
        # code does, not what reads naturally.
        ids = [
            "task:1:comment:9",
            "task:1:title_content",
            "task:1:comment:10",
            "task:1:comment:2",
        ]
        self.assertEqual(self._upsert(ids), sorted(ids))

    def test_lock_order_survives_a_hostile_iteration_order(self):
        """The regression guard — stands in for the second process.

        `_HostileSet` hands out the ids in exactly the wrong order, the way
        a differently-salted process would. `sorted` must override it.
        """
        ids = [f"task:1:comment:{n}" for n in range(12)]
        self.assertEqual(self._upsert(ids, container=_HostileSet), sorted(ids))

    def test_lock_order_does_not_depend_on_how_the_set_was_built(self):
        ids = [f"task:1:comment:{n}" for n in range(12)]
        first = self._upsert(ids)
        RagChunk.objects.all().delete()
        second = self._upsert(list(reversed(ids)))
        self.assertEqual(first, second)

    def test_every_affected_chunk_is_still_written(self):
        """Ordering must not change WHICH rows are mirrored."""
        ids = ["task:1:comment:3", "task:1:title_content", "task:1:comment:1"]
        self._upsert(ids)
        self.assertEqual(sorted(RagChunk.objects.values_list("chunk_id", flat=True)), sorted(ids))


class BenignBulkErrorTests(TestCase):
    """A concurrent writer's 409 / 404 must not red the cron run.

    `CronCommand` attaches an error tripwire to the `origin` logger and
    raises `CommandError` if anything logs at ERROR, so the log LEVEL is
    the contract under test, not a cosmetic choice.
    """

    def setUp(self):
        super().setUp()
        alias = mock.patch.object(ingestion, "get_index_alias", return_value="rag-test")
        client = mock.patch.object(ingestion, "get_client")
        bulk = mock.patch.object(ingestion.os_helpers, "bulk")
        alias.start()
        client.start()
        self.bulk = bulk.start()
        for p in (alias, client, bulk):
            self.addCleanup(p.stop)

    @staticmethod
    def _conflict(cid):
        return {
            "update": {
                "_id": cid,
                "status": 409,
                "error": {
                    "type": "version_conflict_engine_exception",
                    "reason": f"[{cid}]: version conflict, required seqNo [1]",
                },
            }
        }

    def test_version_conflict_does_not_log_an_error(self):
        self.bulk.return_value = (2, [self._conflict("task:1:a")])
        with self.assertLogs(ingestion.log, level="WARNING") as cm:
            ingestion._bulk_index([{"_op_type": "update", "_id": "task:1:a"}])
        self.assertFalse(
            [r for r in cm.records if r.levelno >= logging.ERROR],
            "a 409 from a concurrent writer must not red the cron run",
        )
        self.assertTrue(any("concurrent writer" in r.getMessage() for r in cm.records))

    def test_a_real_bulk_error_still_logs_an_error(self):
        """The other half: the downgrade must not swallow genuine failures."""
        self.bulk.return_value = (
            0,
            [
                {
                    "update": {
                        "_id": "task:1:a",
                        "status": 400,
                        "error": {"type": "mapper_parsing_exception"},
                    }
                }
            ],
        )
        with self.assertLogs(ingestion.log, level="ERROR") as cm:
            ingestion._bulk_index([{"_op_type": "update", "_id": "task:1:a"}])
        self.assertTrue([r for r in cm.records if r.levelno >= logging.ERROR])

    def test_mixed_batch_reports_only_the_real_error(self):
        self.bulk.return_value = (
            1,
            [
                self._conflict("task:1:a"),
                {"update": {"_id": "task:1:b", "status": 500, "error": {"type": "boom"}}},
            ],
        )
        with self.assertLogs(ingestion.log, level="WARNING") as cm:
            ingestion._bulk_index([{"_op_type": "update", "_id": "task:1:a"}])
        errors = [r for r in cm.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 1)
        # The count must exclude the benign one, or the operator is sent
        # looking for a conflict that was never a problem.
        self.assertIn("1 errors", errors[0].getMessage())

    def test_a_409_that_is_not_a_version_conflict_still_logs_an_error(self):
        """Narrow on the error type, not just the status code.

        409 is also how a `create` op reports "document already exists",
        which for a chunk we believe is new means the tracking table and
        the index disagree — worth a red run, unlike a version conflict.
        """
        self.bulk.return_value = (
            0,
            [
                {
                    "create": {
                        "_id": "task:1:a",
                        "status": 409,
                        "error": {"type": "document_already_exists_exception"},
                    }
                }
            ],
        )
        with self.assertLogs(ingestion.log, level="ERROR") as cm:
            ingestion._bulk_index([{"_op_type": "create", "_id": "task:1:a"}])
        self.assertTrue([r for r in cm.records if r.levelno >= logging.ERROR])

    def test_stale_delete_404_does_not_log_an_error(self):
        """A 404 on delete is the outcome the delete wanted."""
        RagChunk.objects.create(
            chunk_id="task:1:gone",
            entity_type="task",
            entity_id="task:1",
            chunk_type="task_comment",
            team_id=TEAM_ID,
            text_hash="h",
            meta_hash="m",
            embedding_model="test-embed-v1",
        )
        self.bulk.return_value = (0, [{"delete": {"_id": "task:1:gone", "status": 404}}])
        stats = ingestion.IngestionStats()
        with mock.patch.object(ingestion.log, "error") as err:
            ingestion._delete_stale("task", "task:1", keep_chunk_ids=set(), stats=stats)
        err.assert_not_called()
        # The tracking row still goes, or the next pass retries forever.
        self.assertFalse(RagChunk.objects.filter(chunk_id="task:1:gone").exists())
        self.assertEqual(stats.chunks_deleted, 1)

    def test_a_real_delete_error_still_logs_an_error(self):
        RagChunk.objects.create(
            chunk_id="task:1:stuck",
            entity_type="task",
            entity_id="task:1",
            chunk_type="task_comment",
            team_id=TEAM_ID,
            text_hash="h",
            meta_hash="m",
            embedding_model="test-embed-v1",
        )
        self.bulk.return_value = (
            0,
            [{"delete": {"_id": "task:1:stuck", "status": 503, "error": {"type": "boom"}}}],
        )
        stats = ingestion.IngestionStats()
        with self.assertLogs(ingestion.log, level="ERROR") as cm:
            ingestion._delete_stale("task", "task:1", keep_chunk_ids=set(), stats=stats)
        self.assertTrue([r for r in cm.records if r.levelno >= logging.ERROR])

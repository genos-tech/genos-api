"""Scheduled OpenSearch index maintenance (deleted-doc reclamation).

Why this exists: the chunk index is continuously *updated*, not just
appended to. Every re-ingest of a changed chunk is a Lucene
delete + re-add, and every purge (delete hooks + the orphan sweep) is a
delete — so "deleted" documents accumulate inside segments. Lucene only
reclaims them opportunistically during natural merges, and the k-NN lane
makes the cost worse than plain BM25 bloat: deleted vectors stay in each
segment's HNSW graph and are visited during graph traversal, then
filtered from results — so query latency and disk both degrade as the
deleted ratio climbs.

The fix is the standard one for update-heavy indices: a periodic
`_forcemerge?only_expunge_deletes=true`, which rewrites only the
segments whose own deleted ratio exceeds the engine threshold
(`index.merge.policy.expunge_deletes_allowed`, default 10%). Unlike a
full `max_num_segments=1` force merge, expunge is safe on an index that
is still being written to — it never produces the one-giant-segment
shape that stops participating in natural merges.

Called from the `opensearch_maintain` management command (daily cron on
Railway / Cloud Scheduler). The whole-index deleted ratio gates the call
so quiet days cost one stats request and a log line, not a merge.
"""

from __future__ import annotations

import logging
from typing import Optional

from opensearchpy.exceptions import ConnectionTimeout

from origin.search_engine.opensearch_client import get_client, get_index_alias

log = logging.getLogger(__name__)

# Skip the expunge below this whole-index deleted-doc share. Segments are
# only rewritten above ~10% deletes each (engine default), so calling
# with the index nearly clean is pure I/O churn.
DEFAULT_MIN_DELETED_RATIO = 0.05

# A merge on a large index can run for minutes. The client waits this
# long; on timeout the merge keeps running server-side (see
# maintain_index), so this is a reporting bound, not a kill switch.
MERGE_REQUEST_TIMEOUT_S = 1800


def collect_index_stats(client, index) -> dict:
    """Primary-shard stats for the doc/segment health snapshot."""
    resp = client.indices.stats(index=index, metric=["docs", "store", "segments"])
    primaries = resp.get("_all", {}).get("primaries", {})
    docs = primaries.get("docs", {})
    live = docs.get("count", 0)
    deleted = docs.get("deleted", 0)
    total = live + deleted
    return {
        "docs": live,
        "deleted_docs": deleted,
        "deleted_ratio": round(deleted / total, 4) if total else 0.0,
        "store_bytes": primaries.get("store", {}).get("size_in_bytes", 0),
        "segments": primaries.get("segments", {}).get("count", 0),
    }


def maintain_index(
    *,
    min_deleted_ratio: float = DEFAULT_MIN_DELETED_RATIO,
    force: bool = False,
    max_num_segments: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """One maintenance pass. Returns a report dict for the cron log:
    `{"action": ..., "before": stats, "after": stats-or-None}`.

    Actions:
      * `expunge_deletes` — ran the merge (the normal daily outcome).
      * `full_merge`      — `max_num_segments` compaction (manual only:
        run it right after a full `--recreate` reindex, never on the
        daily cron — a maxed-out segment stops merging naturally).
      * `skipped_below_threshold` / `skipped_dry_run` — stats only.
      * `index_missing`   — alias not there (e.g. OpenSearch restarted
        with ephemeral storage and the reindexer hasn't recreated it
        yet). A warning, not an error: the reindexer owns recovery.
      * `merge_timed_out` — client gave up waiting; the merge continues
        server-side, so this is logged as a warning (an ERROR would trip
        the CronCommand tripwire and red a run that actually succeeded).
    """
    client = get_client()
    alias = get_index_alias()

    if not client.indices.exists_alias(name=alias):
        log.warning(
            "Maintenance skipped: alias %r does not exist yet. "
            "The reindexer cron recreates it via opensearch_setup.",
            alias,
        )
        return {"action": "index_missing", "before": None, "after": None}

    before = collect_index_stats(client, alias)
    report = {"action": None, "before": before, "after": None}

    if dry_run:
        report["action"] = "skipped_dry_run"
        return report

    if max_num_segments is not None:
        merge_kwargs = {"max_num_segments": max_num_segments}
        report["action"] = "full_merge"
    elif force or before["deleted_ratio"] >= min_deleted_ratio:
        merge_kwargs = {"only_expunge_deletes": True}
        report["action"] = "expunge_deletes"
    else:
        report["action"] = "skipped_below_threshold"
        return report

    try:
        client.indices.forcemerge(
            index=alias,
            request_timeout=MERGE_REQUEST_TIMEOUT_S,
            **merge_kwargs,
        )
    except ConnectionTimeout:
        log.warning(
            "Force merge exceeded the %ss client timeout; the merge "
            "continues server-side and reclaimed space will show in the "
            "next run's stats.",
            MERGE_REQUEST_TIMEOUT_S,
        )
        report["action"] = "merge_timed_out"
        return report

    report["after"] = collect_index_stats(client, alias)
    return report

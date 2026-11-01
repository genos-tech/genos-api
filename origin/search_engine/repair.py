"""Drift between the `RagChunk` tracking table and the OpenSearch index.

The indexer trusts `RagChunk`: a chunk whose row matches the incoming
hashes is `unchanged` and never touched again. So when the index loses a
document the table believes in (a write that failed before MIP-855 stopped
recording it, an index recreated or restored under a live table), neither
the cron nor a full `opensearch_reindex` heals it. This module finds that
drift and clears the rows that cause it, so the NORMAL pass re-embeds
exactly those chunks.

  * tracked, but not in the index         -> drop the row  (re-embed)
  * tracked, but the stored `text_hash`, `embedding_model` or
    `index_schema_version` differ from the row -> drop the row  (re-embed)
  * in the index, but not tracked         -> reported only. A document is
    written before its row, so a live ingest looks the same; and deleting
    on a guess is how an index gets emptied.

Metadata (status, assignee, ACL, ...) can't be compared: `meta_hash` isn't
stored in OpenSearch. `blank_meta_hashes` is the blunt tool for it: blank
the hash and the next pass re-sends every overlay as a partial update,
which costs bulk updates and no embeddings.

Comparison uses `mget`, which is realtime. A `search` only sees refreshed
segments, so a document written seconds ago would be reported missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from opensearchpy import helpers as os_helpers

from origin.search_engine.models import RagChunk
from origin.search_engine.opensearch_client import get_client, get_index_alias

log = logging.getLogger(__name__)

_BATCH = 500
# The stored fields compared with the row. Nothing else is fetched: no text,
# no vector.
_COMPARED = ("text_hash", "embedding_model", "index_schema_version")
_SAMPLE = 10


@dataclass
class Drift:
    """Chunk ids by kind of drift. `*_ids` are what `apply` acts on."""

    scanned: int = 0
    missing_ids: list[str] = field(default_factory=list)
    mismatch_ids: list[str] = field(default_factory=list)
    # A stored field absent from the document (written before it existed):
    # unknown, not a mismatch. Counted so the operator sees it; never acted on.
    unverifiable: int = 0
    untracked_ids: list[str] = field(default_factory=list)
    untracked_checked: bool = False
    entity_types: dict[str, int] = field(default_factory=dict)

    @property
    def repairable_ids(self) -> list[str]:
        return self.missing_ids + self.mismatch_ids

    def as_dict(self) -> dict:
        return {
            "tracked_scanned": self.scanned,
            "tracked_missing_in_index": len(self.missing_ids),
            "tracked_stale_in_index": len(self.mismatch_ids),
            "tracked_unverifiable": self.unverifiable,
            "untracked_in_index": (
                len(self.untracked_ids) if self.untracked_checked else "not checked"
            ),
            "repairable_by_entity_type": dict(sorted(self.entity_types.items())),
            "sample_missing": self.missing_ids[:_SAMPLE],
            "sample_stale": self.mismatch_ids[:_SAMPLE],
            "sample_untracked": self.untracked_ids[:_SAMPLE],
        }


def _tracked(entity_types: Optional[Iterable[str]]):
    qs = RagChunk.objects.all()
    if entity_types:
        qs = qs.filter(entity_type__in=list(entity_types))
    return qs


def find_drift(
    entity_types: Optional[Iterable[str]] = None, *, check_untracked: bool = True
) -> Drift:
    """Compare tracking rows with the index. Read-only on both sides.

    Raises when OpenSearch can't be read: a half-answered comparison must
    never be mistaken for "no drift"."""
    client = get_client()
    alias = get_index_alias()
    drift = Drift()
    types = list(entity_types) if entity_types else None

    rows = (
        _tracked(types)
        .order_by("chunk_id")
        .only("chunk_id", "entity_type", "text_hash", "embedding_model", "index_schema_version")
    )
    batch: list[RagChunk] = []

    def flush() -> None:
        if batch:
            _compare_batch(client, alias, batch, drift)
            batch.clear()

    for row in rows.iterator(chunk_size=_BATCH):
        batch.append(row)
        if len(batch) >= _BATCH:
            flush()
    flush()

    if check_untracked:
        drift.untracked_checked = True
        drift.untracked_ids = _find_untracked(client, alias, types)
    return drift


def _compare_batch(client, alias: str, rows: list[RagChunk], drift: Drift) -> None:
    resp = client.mget(
        index=alias,
        body={"ids": [r.chunk_id for r in rows]},
        _source_includes=",".join(_COMPARED),
    )
    docs = {d.get("_id"): d for d in resp.get("docs", [])}
    for row in rows:
        drift.scanned += 1
        doc = docs.get(row.chunk_id)
        if doc is None or "error" in doc:
            # An item-level error is "couldn't read it", not "it's gone".
            # Treating it as missing would drop a healthy row.
            if doc is not None:
                raise RuntimeError(f"mget failed for {row.chunk_id}: {doc.get('error')}")
            _flag(drift, drift.missing_ids, row)
        elif not doc.get("found"):
            _flag(drift, drift.missing_ids, row)
        else:
            src = doc.get("_source") or {}
            stored = {k: src.get(k) for k in _COMPARED}
            if any(v is None for v in stored.values()):
                drift.unverifiable += 1
            elif any(stored[k] != getattr(row, k) for k in _COMPARED):
                _flag(drift, drift.mismatch_ids, row)


def _flag(drift: Drift, bucket: list[str], row: RagChunk) -> None:
    bucket.append(row.chunk_id)
    drift.entity_types[row.entity_type] = drift.entity_types.get(row.entity_type, 0) + 1


def _find_untracked(client, alias: str, types: Optional[list[str]]) -> list[str]:
    query: dict = {"match_all": {}}
    if types:
        query = {"terms": {"entity_type": types}}
    out: list[str] = []
    batch: list[str] = []

    def flush() -> None:
        if not batch:
            return
        known = set(RagChunk.objects.filter(chunk_id__in=batch).values_list("chunk_id", flat=True))
        out.extend(cid for cid in batch if cid not in known)
        batch.clear()

    for hit in os_helpers.scan(
        client, index=alias, query={"query": query, "_source": False}, size=_BATCH
    ):
        batch.append(hit["_id"])
        if len(batch) >= _BATCH:
            flush()
    flush()
    return out


def apply(drift: Drift) -> int:
    """Drop the tracking rows of every missing / stale chunk, so the next
    ordinary pass sees them as `new` and re-embeds them. Returns the number
    of rows dropped. Never touches OpenSearch."""
    ids = drift.repairable_ids
    dropped = 0
    for i in range(0, len(ids), _BATCH):
        dropped += RagChunk.objects.filter(chunk_id__in=ids[i : i + _BATCH]).delete()[0]
    log.info("Repair: dropped %d tracking row(s) for re-embedding", dropped)
    return dropped


def blank_meta_hashes(entity_types: Optional[Iterable[str]] = None) -> int:
    """Blank `meta_hash` on every tracked chunk (of `entity_types`), so the
    next pass re-sends their overlays as partial updates. No embeddings."""
    return _tracked(entity_types).update(meta_hash="")

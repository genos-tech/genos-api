"""Hybrid search service.

Pipeline:

    user query
        ├─ keyword search (BM25 over title/snippet/search_text)
        ├─ vector search  (k-NN over `embedding`)
        ↓
    Reciprocal Rank Fusion (RRF)
        ↓
    Group by `entity_type:entity_id`, take the best chunk per entity
        ↓
    Top-N results

Filters applied at OpenSearch query time:
  * team_id (mandatory tenant boundary)
  * acl_user_ids contains the requesting user_id
  * entity_types subset (optional)
  * updated_at range (optional)

Both keyword and vector queries return up to `pool_size` chunk hits
each (default 60). The wider pool gives RRF more material to fuse.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from opensearchpy.exceptions import NotFoundError

from origin.search_engine.embeddings import embed_one
from origin.search_engine.opensearch_client import get_client, get_index_alias

log = logging.getLogger(__name__)


RRF_K = 60
DEFAULT_POOL_SIZE = 60
DEFAULT_LIMIT = 20


def search(
    *,
    query: str,
    team_id: str,
    user_id: str,
    entity_types: Optional[list[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    pool_size: int = DEFAULT_POOL_SIZE,
    use_vector: bool = True,
) -> dict:
    """Run a hybrid search and return entity-grouped results.

    Args:
        query: user-supplied query string.
        team_id: tenant — required.
        user_id: requesting user — used for ACL filter.
        entity_types: subset, e.g. ["chat","note"]. Default: all.
        date_from/date_to: ISO 8601 strings (compared against `updated_at`).
        limit: number of entity-level results to return.
        pool_size: raw chunk pool size per search lane.
        use_vector: if False, skip vector lane (keyword-only fallback —
            useful when no OPENAI_API_KEY is set).
    """
    if not query or not query.strip():
        return {"query": query, "results": []}

    client = get_client()
    index = get_index_alias()

    base_filter = _build_filter(team_id, user_id, entity_types, date_from, date_to)

    # --- Keyword lane ---
    keyword_hits = _run_keyword(client, index, query, base_filter, pool_size)

    # --- Vector lane ---
    vector_hits: list[dict] = []
    if use_vector:
        try:
            qvec = embed_one(query)
            vector_hits = _run_vector(client, index, qvec, base_filter, pool_size)
        except Exception as e:  # noqa: BLE001 — degrade to keyword-only
            log.warning("Vector search failed, falling back to keyword-only: %s", e)

    # --- RRF fuse ---
    fused = _rrf_fuse(keyword_hits, vector_hits)

    # --- Group by entity ---
    grouped = _group_by_entity(fused)

    # --- Top N ---
    grouped.sort(key=lambda x: x["score"], reverse=True)
    return {"query": query, "results": grouped[:limit]}


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _build_filter(
    team_id: str,
    user_id: str,
    entity_types: Optional[list[str]],
    date_from: Optional[str],
    date_to: Optional[str],
) -> list[dict]:
    filt: list[dict] = [
        {"term": {"team_id": team_id}},
        {"term": {"acl_user_ids": user_id}},
    ]
    if entity_types:
        filt.append({"terms": {"entity_type": entity_types}})
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["gte"] = date_from
        if date_to:
            rng["lte"] = date_to
        filt.append({"range": {"updated_at": rng}})
    return filt


def _run_keyword(client, index: str, query: str, base_filter: list[dict], size: int) -> list[dict]:
    body = {
        "size": size,
        "query": {
            "bool": {
                "must": {
                    "multi_match": {
                        "query": query,
                        "fields": [
                            "title^3",
                            "snippet_text^2",
                            "search_text",
                        ],
                        "type": "best_fields",
                    }
                },
                "filter": base_filter,
            }
        },
        "_source": _source_fields(),
    }
    try:
        resp = client.search(index=index, body=body)
    except NotFoundError:
        return []
    return list(resp.get("hits", {}).get("hits", []))


def _run_vector(
    client, index: str, qvec: list[float], base_filter: list[dict], size: int
) -> list[dict]:
    body = {
        "size": size,
        "query": {
            "bool": {
                "must": {
                    "knn": {
                        "embedding": {
                            "vector": qvec,
                            "k": size,
                        }
                    }
                },
                "filter": base_filter,
            }
        },
        "_source": _source_fields(),
    }
    try:
        resp = client.search(index=index, body=body)
    except NotFoundError:
        return []
    return list(resp.get("hits", {}).get("hits", []))


def _source_fields() -> list[str]:
    return [
        "chunk_id",
        "entity_type",
        "entity_id",
        "chunk_type",
        "title",
        "snippet_text",
        "chat_type",
        "chat_id",
        "thread_id",
        "task_id",
        "note_id",
        "note_type",
        "project_id",
        "related_entity_ids",
        "updated_at",
        "created_at",
    ]


def _rrf_fuse(keyword_hits: list[dict], vector_hits: list[dict]) -> list[dict]:
    """Reciprocal Rank Fusion: combine two ranked chunk-hit lists.

    Returns a list of `{chunk_id, source, score, keyword_rank, vector_rank}`
    sorted by RRF score.
    """
    by_chunk: dict[str, dict] = {}

    for rank, hit in enumerate(keyword_hits, start=1):
        cid = hit["_id"]
        by_chunk.setdefault(
            cid,
            {
                "chunk_id": cid,
                "source": hit["_source"],
                "score": 0.0,
                "keyword_rank": None,
                "vector_rank": None,
            },
        )
        by_chunk[cid]["score"] += 1.0 / (RRF_K + rank)
        by_chunk[cid]["keyword_rank"] = rank
        by_chunk[cid]["source"] = hit["_source"]

    for rank, hit in enumerate(vector_hits, start=1):
        cid = hit["_id"]
        by_chunk.setdefault(
            cid,
            {
                "chunk_id": cid,
                "source": hit["_source"],
                "score": 0.0,
                "keyword_rank": None,
                "vector_rank": None,
            },
        )
        by_chunk[cid]["score"] += 1.0 / (RRF_K + rank)
        by_chunk[cid]["vector_rank"] = rank
        by_chunk[cid]["source"] = hit["_source"]

    return sorted(by_chunk.values(), key=lambda x: x["score"], reverse=True)


def _group_by_entity(fused_chunks: list[dict]) -> list[dict]:
    """Collapse chunk-level hits into entity-level rows.

    Per entity we keep:
      * highest chunk score → entity score
      * all chunk types that matched
      * the highest-ranked chunk's snippet
    """
    by_entity: dict[tuple[str, str], dict] = {}
    for c in fused_chunks:
        src = c["source"]
        key = (src.get("entity_type"), src.get("entity_id"))
        existing = by_entity.get(key)
        if existing is None:
            by_entity[key] = {
                "entity_type": src.get("entity_type"),
                "entity_id": src.get("entity_id"),
                "title": src.get("title"),
                "best_matched_chunk_id": c["chunk_id"],
                "matched_chunk_types": [src.get("chunk_type")] if src.get("chunk_type") else [],
                "snippet": src.get("snippet_text"),
                "score": c["score"],
                "keyword_rank": c["keyword_rank"],
                "vector_rank": c["vector_rank"],
                "updated_at": src.get("updated_at"),
                "chat_type": src.get("chat_type"),
                "chat_id": src.get("chat_id"),
                "thread_id": src.get("thread_id"),
                "task_id": src.get("task_id"),
                "note_id": src.get("note_id"),
                "note_type": src.get("note_type"),
                "project_id": src.get("project_id"),
                "related_entity_ids": src.get("related_entity_ids") or [],
            }
        else:
            chunk_type = src.get("chunk_type")
            if chunk_type and chunk_type not in existing["matched_chunk_types"]:
                existing["matched_chunk_types"].append(chunk_type)
            # Keep the highest score — `fused_chunks` is already sorted
            # by score desc, so the first occurrence wins.
    return list(by_entity.values())

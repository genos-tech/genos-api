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

# Default relevance threshold relative to the top result's RRF score.
# Anything below `top_score * MIN_SCORE_RATIO` is treated as a weak
# match and dropped, even if it would otherwise fit under `limit`. So
# a query with one strong hit and a long tail of near-noise returns
# just the strong hit, but a query with several near-tied hits returns
# all of them.
#
# Why a ratio instead of an absolute number: RRF scores are bounded
# above by 1/(RRF_K+1) ≈ 0.016 per lane (so ≤ 0.033 with both lanes),
# but the *useful* range depends on how many lanes fired and how the
# query distributes across them. A fixed absolute threshold would
# misbehave when only one lane is active (e.g. when OPENAI_API_KEY is
# missing and vector search is skipped).
DEFAULT_MIN_SCORE_RATIO = 0.5

# Absolute minimum: anything below this is noise regardless of the top
# score. Useful when the top score itself is barely above zero (e.g.
# the only "matches" came in at rank 50+). Tunable per call.
DEFAULT_MIN_SCORE = 1.0 / (RRF_K + 30)  # ≈ 0.011 — a single lane hit at rank ≥ 30


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
    min_score_ratio: float = DEFAULT_MIN_SCORE_RATIO,
    min_score: float = DEFAULT_MIN_SCORE,
    for_agent: bool = False,
    max_chunks_per_entity: int = 3,
) -> dict:
    """Run a hybrid search and return entity-grouped results.

    Args:
        query: user-supplied query string.
        team_id: tenant — required.
        user_id: requesting user — used for ACL filter.
        entity_types: subset, e.g. ["chat","note"]. Default: all.
        date_from/date_to: ISO 8601 strings (compared against `updated_at`).
        limit: max number of entity-level results to return (after
            relevance filtering).
        pool_size: raw chunk pool size per search lane.
        use_vector: if False, skip vector lane (keyword-only fallback —
            useful when no OPENAI_API_KEY is set).
        min_score_ratio: drop results whose RRF score is below
            `top_score * min_score_ratio`. Pass 0 to disable. Default
            0.5 — meaning we only return results within ~half the top
            result's confidence.
        min_score: absolute floor on the RRF score. Pass 0 to disable.
            Default trims pure-noise matches (single lane, rank ≥ 30).
        for_agent: if True, return a richer shape suitable for stuffing
            into an LLM prompt: includes `search_text` (the full chunk
            text) and up to `max_chunks_per_entity` matched chunks per
            entity. The UI-facing shape (snippet only, one chunk per
            entity) is the default to keep wire size small.
        max_chunks_per_entity: when `for_agent=True`, cap on how many
            chunks per entity are returned. Default 3 — keeps prompt
            size bounded but gives the LLM more than just the snippet.
    """
    if not query or not query.strip():
        return {"query": query, "results": []}

    client = get_client()
    index = get_index_alias()

    base_filter = _build_filter(team_id, user_id, entity_types, date_from, date_to)

    # --- Keyword lane ---
    keyword_hits = _run_keyword(client, index, query, base_filter, pool_size, for_agent=for_agent)

    # --- Vector lane ---
    vector_hits: list[dict] = []
    if use_vector:
        try:
            qvec = embed_one(query)
            vector_hits = _run_vector(
                client, index, qvec, base_filter, pool_size, for_agent=for_agent
            )
        except Exception as e:  # noqa: BLE001 — degrade to keyword-only
            log.warning("Vector search failed, falling back to keyword-only: %s", e)

    # --- RRF fuse ---
    fused = _rrf_fuse(keyword_hits, vector_hits)

    # --- Group by entity ---
    grouped = _group_by_entity(
        fused, for_agent=for_agent, max_chunks_per_entity=max_chunks_per_entity
    )

    # --- Sort, apply relevance threshold, truncate to limit. ---
    grouped.sort(key=lambda x: x["score"], reverse=True)
    grouped = _apply_relevance_threshold(grouped, min_score_ratio, min_score)
    return {"query": query, "results": grouped[:limit]}


def _apply_relevance_threshold(
    grouped: list[dict], min_score_ratio: float, min_score: float
) -> list[dict]:
    """Drop results that are weak in absolute or relative terms.

    `grouped` must already be sorted by score desc.
    """
    if not grouped:
        return grouped
    top_score = grouped[0]["score"]
    relative_floor = top_score * min_score_ratio if min_score_ratio > 0 else 0.0
    floor = max(relative_floor, min_score)
    if floor <= 0:
        return grouped
    return [g for g in grouped if g["score"] >= floor]


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


def _run_keyword(
    client, index: str, query: str, base_filter: list[dict], size: int, *, for_agent: bool = False
) -> list[dict]:
    body = {
        "size": size,
        "_source": _source_fields(for_agent=for_agent),
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
    }
    try:
        resp = client.search(index=index, body=body)
    except NotFoundError:
        return []
    return list(resp.get("hits", {}).get("hits", []))


def _run_vector(
    client,
    index: str,
    qvec: list[float],
    base_filter: list[dict],
    size: int,
    *,
    for_agent: bool = False,
) -> list[dict]:
    body = {
        "size": size,
        "_source": _source_fields(for_agent=for_agent),
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
    }
    try:
        resp = client.search(index=index, body=body)
    except NotFoundError:
        return []
    return list(resp.get("hits", {}).get("hits", []))


def _source_fields(*, for_agent: bool = False) -> list[str]:
    fields = [
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
    if for_agent:
        # The full chunk text — used as LLM grounding context. Excluded
        # from the UI-facing shape to keep wire size small (the UI only
        # needs `snippet_text`).
        fields.append("search_text")
    return fields


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


def _group_by_entity(
    fused_chunks: list[dict],
    *,
    for_agent: bool = False,
    max_chunks_per_entity: int = 3,
) -> list[dict]:
    """Collapse chunk-level hits into entity-level rows.

    Per entity we keep:
      * highest chunk score → entity score
      * all chunk types that matched
      * the highest-ranked chunk's snippet

    When `for_agent=True`, also attach a `chunks` list with up to
    `max_chunks_per_entity` matched chunks (each with `chunk_id`,
    `chunk_type`, and `text`) so the caller can stuff full chunk text
    into an LLM prompt instead of only the short snippet.
    """
    by_entity: dict[tuple[str, str], dict] = {}
    for c in fused_chunks:
        src = c["source"]
        key = (src.get("entity_type"), src.get("entity_id"))
        existing = by_entity.get(key)
        if existing is None:
            entry = {
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
            if for_agent:
                entry["chunks"] = [_chunk_for_agent(c)]
            by_entity[key] = entry
        else:
            chunk_type = src.get("chunk_type")
            if chunk_type and chunk_type not in existing["matched_chunk_types"]:
                existing["matched_chunk_types"].append(chunk_type)
            if for_agent and len(existing.get("chunks", [])) < max_chunks_per_entity:
                existing["chunks"].append(_chunk_for_agent(c))
            # Keep the highest score — `fused_chunks` is already sorted
            # by score desc, so the first occurrence wins.
    return list(by_entity.values())


def _chunk_for_agent(c: dict) -> dict:
    src = c["source"]
    return {
        "chunk_id": c["chunk_id"],
        "chunk_type": src.get("chunk_type"),
        "text": src.get("search_text") or src.get("snippet_text") or "",
        "score": c["score"],
    }

"""Embedding provider package — factory + public surface.

Public functions (`embed_texts`, `embed_one`, `hash_text`) preserve the
historical embeddings.py API so existing callers in ingestion.py,
search.py, and agent/evals/runner.py don't change.

Provider is chosen by `SEARCH_ENGINE["EMBEDDING_PROVIDER"]`:
    "openai" (default) → `OpenAIEmbedder`
    "vertex"           → `VertexEmbedder` (reuses GEMINI_USE_VERTEX auth)

`embed_one` keeps the bounded LRU cache that the Spotlight typeahead
hot path relies on: backspacing "hello" → "hell" → "hello" costs one
roundtrip instead of three. Cache key is `(model_name, text)`; the
model name differs across providers so a provider/model swap simply
ages the old entries out of the LRU.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Iterable

from django.conf import settings

from origin.search_engine.embeddings.base import Embedder, TaskType


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """Return one embedding per input text, preserving order.

    Empty strings are sanitised to a single space so the provider API
    doesn't reject the batch; the caller is expected to filter zero /
    placeholder vectors before indexing.
    """
    texts = list(texts)
    if not texts:
        return []
    sanitized = [t if t and t.strip() else " " for t in texts]
    return _get_embedder().embed(sanitized, task_type="document")


def embed_one(text: str) -> list[float]:
    """Embed a single string. Cached on `(model, text)` so repeated
    or near-repeated query embeddings inside one Django worker hit
    memory instead of the provider API. The Spotlight typeahead path
    goes through here on every keystroke.

    Empty / whitespace-only input is intentionally skipped — the
    sanitisation logic in `embed_texts` produces a placeholder vector
    that we don't want to keep in the cache.
    """
    if not text or not text.strip():
        return embed_texts([text])[0]
    embedder = _get_embedder()
    # Cache stores immutable tuples; return a fresh list so callers
    # can't accidentally mutate the cached entry.
    return list(_embed_one_cached(embedder.model_name, text))


# Bounded LRU. 256 entries is plenty for one user's typing burst
# (each prefix of a 16-char query is at most 16 entries) and small
# enough that 1536-dim float lists won't dominate worker memory:
# 256 entries * 1536 floats * 8 bytes ≈ 3 MB.
@lru_cache(maxsize=256)
def _embed_one_cached(model: str, text: str) -> tuple:
    """Underlying cached single-text embedder. Keys on `(model, text)`
    so a model swap (provider change, dimension change, version bump)
    doesn't return stale vectors — old entries simply age out of the
    LRU. `task_type` is implicit ("query" for every cached call) so
    it's not in the key."""
    embedder = _get_embedder()
    return tuple(embedder.embed([text], task_type="query")[0])


def hash_text(text: str) -> str:
    """SHA-256 of the input text. Used to skip re-embedding unchanged chunks."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_active_embedding_model_name() -> str:
    """Model identifier of the currently-active provider. Read by
    `ingestion.py` to populate `RagChunk.embedding_model`, which drives
    the re-embed mismatch check."""
    return _get_embedder().model_name


def get_active_embedding_dimensions() -> int:
    """Vector dim of the currently-active provider. Read by
    `index_config.build_mappings()` so the OpenSearch `knn_vector`
    mapping always matches what we'll write into it."""
    return _get_embedder().dimensions


def _get_embedder() -> Embedder:
    """Return the configured `Embedder` adapter.

    Lazily imports each adapter so a deploy that only uses one provider
    doesn't pay the import cost (and a missing SDK for an unused
    provider doesn't break the app). Raises `RuntimeError` for an
    unknown value rather than silently falling back, so a typo in the
    env var surfaces immediately.
    """
    provider = (settings.SEARCH_ENGINE.get("EMBEDDING_PROVIDER") or "openai").lower()
    if provider == "openai":
        from origin.search_engine.embeddings.openai_embedder import OpenAIEmbedder  # noqa: PLC0415

        return OpenAIEmbedder()
    if provider == "vertex":
        from origin.search_engine.embeddings.vertex_embedder import VertexEmbedder  # noqa: PLC0415

        return VertexEmbedder()
    raise RuntimeError(
        f"Unknown EMBEDDING_PROVIDER {provider!r}. "
        "Set SEARCH_ENGINE['EMBEDDING_PROVIDER'] to 'openai' or 'vertex'."
    )


__all__ = [
    "Embedder",
    "TaskType",
    "embed_one",
    "embed_texts",
    "get_active_embedding_dimensions",
    "get_active_embedding_model_name",
    "hash_text",
]

"""OpenAI embedding wrapper.

Batches calls to the /v1/embeddings endpoint and handles transient
errors with a couple of retries. Embeddings are pre-normalized by the
OpenAI text-embedding-3 family, so we can use cosine similarity in
OpenSearch directly.

`embed_one` is additionally backed by an in-memory LRU cache so the
Spotlight typeahead hot path doesn't pay a ~200-500 ms OpenAI roundtrip
on every keystroke. Backspacing, re-typing the same query, or two
users in the same Django worker firing the same search all hit the
cache. Chunk indexing is unaffected — `embed_texts` always goes to
the API (RagChunk-level dedup already prevents wasted re-embeds for
unchanged chunks).
"""

import hashlib
import logging
import time
from functools import lru_cache
from typing import Iterable

from django.conf import settings
from openai import OpenAI

logger = logging.getLogger(__name__)


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = settings.SEARCH_ENGINE["OPENAI_API_KEY"]
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured. Set it in the environment "
            "before running indexing or query embedding."
        )
    _client = OpenAI(api_key=api_key)
    return _client


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """Return one embedding per input text, preserving order.

    Empty strings get a None-equivalent placeholder. The caller should
    skip those chunks.
    """
    texts = list(texts)
    if not texts:
        return []

    model = settings.SEARCH_ENGINE["OPENAI_EMBEDDING_MODEL"]
    batch_size = settings.SEARCH_ENGINE["EMBEDDING_BATCH_SIZE"]
    client = _get_client()

    results: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        # Replace empty strings with a single space to satisfy the API
        # (it rejects empty input). Caller is expected to filter zero
        # vectors before indexing.
        sanitized = [t if t and t.strip() else " " for t in batch]
        vectors = _embed_with_retry(client, sanitized, model)
        results.extend(vectors)
    return results


def embed_one(text: str) -> list[float]:
    """Embed a single string. Caches on `(model, text)` so repeated or
    near-repeated query embeddings inside one Django worker hit memory
    instead of OpenAI. The Spotlight typeahead path goes through here
    on every keystroke; without the cache, backspacing "hello" → "hell"
    → "hello" costs three roundtrips for one effective query.

    Empty / whitespace-only input is intentionally skipped — the
    sanitisation logic in `embed_texts` produces a placeholder vector
    that we don't want to keep in the cache.
    """
    if not text or not text.strip():
        return embed_texts([text])[0]
    model = settings.SEARCH_ENGINE["OPENAI_EMBEDDING_MODEL"]
    # Cache stores immutable tuples; return a fresh list so callers
    # can't accidentally mutate the cached entry.
    return list(_embed_one_cached(model, text))


# Bounded LRU. 256 entries is plenty for one user's typing burst
# (each prefix of a 16-char query is at most 16 entries) and small
# enough that 768-dim float lists won't dominate worker memory: 256
# entries * 1536 floats * 8 bytes ≈ 3 MB.
@lru_cache(maxsize=256)
def _embed_one_cached(model: str, text: str) -> tuple:
    """Underlying cached single-text embedder. Keys on `(model, text)`
    so a model swap (e.g. upgrading to text-embedding-3-large) doesn't
    return stale vectors — old entries simply age out of the LRU."""
    client = _get_client()
    return tuple(_embed_with_retry(client, [text], model)[0])


def _embed_with_retry(client, batch, model, max_retries=3):
    delay = 1.0
    for attempt in range(max_retries):
        try:
            resp = client.embeddings.create(model=model, input=batch)
            return [d.embedding for d in resp.data]
        except Exception as e:  # noqa: BLE001 — rate-limit, transient net, etc.
            if attempt == max_retries - 1:
                raise
            logger.warning(
                "OpenAI embedding call failed (attempt %d/%d): %s. Retrying in %.1fs.",
                attempt + 1,
                max_retries,
                e,
                delay,
            )
            time.sleep(delay)
            delay *= 2


def hash_text(text: str) -> str:
    """SHA-256 of the input text. Used to skip re-embedding unchanged chunks."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

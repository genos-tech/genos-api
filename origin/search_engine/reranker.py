"""LLM-as-judge reranker.

After hybrid retrieval produces an entity-level result list, this
module asks the configured `ModelClient` (Gemini or Claude, whichever
is active) to re-order the top-K results by actual relevance to the
user's query. Off by default; enabled per deploy via
`SEARCH_ENGINE["RAG_USE_RERANKER"]`.

Why an LLM-as-judge instead of a dedicated cross-encoder model:

  * We already have a provider-neutral `ModelClient` interface from
    Phase 5. Reusing it means no new SDK and no provider lock-in.
  * Cross-encoder rerankers (e.g. Cohere Rerank, BGE) are excellent
    but add another vendor / deployment dependency. We can swap in
    one later behind the same `rerank(...)` function signature.
  * The cost is bounded — input is title + truncated snippet for
    each candidate (~150 chars each × 20 candidates ≈ 3 K tokens
    input, tiny output). Pennies per call.

Graceful degradation: if the model returns malformed JSON, an empty
list, or fails entirely, we return the original ordering unchanged
and log a warning. The reranker should never be the reason a query
crashes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from origin.search_engine.llm import AgentMessage, get_model_client

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a search-result reranker. You receive a user query and a
numbered list of workspace items (chats / tasks / notes). Your job is
to return the items in order from MOST RELEVANT to LEAST RELEVANT for
that query.

Rules:
  - Reply with ONLY a JSON array of integers, like [3, 0, 1].
  - Each integer is the index of an item from the input list (0-based).
  - Drop items that are clearly NOT relevant — do not include their
    indices in your output.
  - Do not invent new indices. Do not duplicate indices.
  - If NO items are relevant to the query, reply with [].
  - Do not include any prose, explanation, or markdown — only the
    JSON array.
"""

# Snippet truncation cap when building the rerank prompt. The model
# only needs a short window to judge relevance; longer snippets bloat
# the prompt for no benefit and trigger Anthropic's token limits faster
# than we'd like.
_SNIPPET_TRUNCATE = 200


def rerank(
    *,
    query: str,
    entities: list[dict[str, Any]],
    input_k: int,
    output_k: int,
) -> list[dict[str, Any]]:
    """Return up to `output_k` entities reordered by LLM-judged relevance.

    Args:
        query:     the user query (the original `search(...)` `query` arg).
        entities:  entity-level results from `search(...)`. Order is the
                   pre-rerank ranking.
        input_k:   how many of the top entities to send to the model.
                   Smaller = cheaper but riskier (relevant items beyond
                   K never get a second look).
        output_k:  cap on how many entities to return after reranking.

    Falls back to `entities[:output_k]` (the pre-rerank order) if:
        * fewer than 2 candidates (nothing to rerank),
        * the model returns malformed / unparseable output,
        * the model raises mid-call.
    """
    if not entities or input_k <= 1 or output_k <= 0:
        return entities[: max(output_k, 0)]

    candidates = entities[:input_k]
    prompt = _build_user_prompt(query, candidates)

    client = get_model_client()
    msgs = [AgentMessage(role="user", text=prompt)]

    try:
        chunks: list[str] = []
        for text, fc in client.generate_step(
            messages=msgs,
            tools=[],
            system_instruction=_SYSTEM_PROMPT,
        ):
            if text:
                chunks.append(text)
            # Function-call output is unexpected (no tools given); ignore.
            if fc is not None:
                log.warning(
                    "Reranker unexpectedly got a function call from the model "
                    "(name=%s) — ignoring",
                    fc.name,
                )
        raw = "".join(chunks).strip()
    except Exception:  # noqa: BLE001 — surface as a fallback, not a crash
        log.exception("Reranker LLM call failed; falling back to pre-rerank order")
        return candidates[:output_k]

    indices = _parse_indices(raw, valid_range=len(candidates))
    if indices is None:
        log.warning(
            "Reranker returned unparseable output (%r); falling back to " "pre-rerank order",
            raw[:200],
        )
        return candidates[:output_k]

    reordered = [candidates[i] for i in indices[:output_k]]
    return reordered


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _build_user_prompt(query: str, candidates: list[dict[str, Any]]) -> str:
    """Build the numbered-list prompt the model sees."""
    lines: list[str] = [f"Query: {query}", "", "Candidates:"]
    for i, e in enumerate(candidates):
        eid = e.get("entity_id") or "?"
        title = (e.get("title") or "").strip()
        snippet = (e.get("snippet") or "").strip()
        # Strip the Phase-4 boundary tags if a snippet was wrapped on
        # its way out of `search_kb` — they're noise for the reranker
        # which already understands the candidates are workspace data.
        snippet = snippet.replace("<workspace_content>", "").replace("</workspace_content>", "")
        snippet = snippet.strip()
        if len(snippet) > _SNIPPET_TRUNCATE:
            snippet = snippet[:_SNIPPET_TRUNCATE].rstrip() + "…"
        lines.append(f"[{i}] {eid} | {title} | {snippet}")
    return "\n".join(lines)


# Match the first JSON array of integers in the model's response.
# Models sometimes wrap their answer in code fences or add a trailing
# period; this regex finds the array anywhere in the string.
_JSON_ARRAY_RE = re.compile(r"\[[\s\d,]*\]")


def _parse_indices(raw: str, *, valid_range: int) -> list[int] | None:
    """Extract a list of valid 0-based indices from the model's reply.

    Returns None if the response doesn't contain a valid array of
    integers, all-in-range and unique. An empty array `[]` is valid
    and means "no candidate is relevant".
    """
    match = _JSON_ARRAY_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None

    out: list[int] = []
    seen: set[int] = set()
    for x in parsed:
        if not isinstance(x, int):
            return None
        if x < 0 or x >= valid_range:
            return None
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

"""Phase 10 — LLM query rewriter.

Expands a user query into N alternative phrasings before running
retrieval. Each variant takes a turn through the keyword + vector
lanes, and the results are RRF-fused across all variants. Chunks
that surface for multiple variants get extra weight, so the more
"agreed-upon" a result is, the higher it ranks.

Flag-gated via `SEARCH_ENGINE["RAG_USE_QUERY_REWRITE"]` (enabled by
default on the agent path; never fires on the typeahead path).
Reuses the Phase-5 `ModelClient` abstraction so it works on both
Gemini and Claude.

Graceful degradation: any error path (LLM failure, malformed
output, empty variants list) returns `[query]` so the caller's
pipeline runs exactly as if rewriting was disabled. A reply that
cannot be read logs a WARNING with its length, shape (`kind=`) and
stop reason, never its text; an empty list is an INFO.
"""

from __future__ import annotations

import ast
import json
import logging
import re

from django.conf import settings

from origin.search_engine.llm import AgentMessage, get_model_client
from origin.search_engine.llm.choice import (
    get_llm_choice,
    subprocess_model_override,
    subprocess_params,
)
from origin.search_engine.llm.spend import spend_purpose
from origin.search_engine.llm.types import CallUsage

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a search query rewriter for a workspace search engine. The
workspace contains the user's chats, tasks, and notes. Given a query,
produce alternative phrasings that retrieve the same intent but use
different words — synonyms, paraphrases, related domain terms.

Rules:
  - Reply with ONLY a JSON array of strings, like ["foo bar", "baz qux"].
  - Do NOT include the user's original query — only the variants.
  - Each variant is a standalone search query, not a sentence describing
    the query.
  - Variants must stay on the same topic. Don't drift to a different
    question.
  - Write every variant in the SAME language and script as the user's
    query (e.g. a Japanese query gets Japanese variants, not English).
    The corpus is indexed in the user's language, so off-language
    variants retrieve nothing and only add noise.
  - Prefer short, keyword-style variants (3–6 words) over long
    natural-language ones.
  - Do not include prose, explanations, code fences, or markdown.
"""


@spend_purpose("rewrite")
def rewrite_query(query: str, *, num_variants: int = 3) -> list[str]:
    """Return `[query, *variants]` — the original first, then expansions.

    Args:
        query:        the user's raw query.
        num_variants: how many LLM-generated variants to request. The
                      caller controls the budget — more variants =
                      more OpenSearch round-trips + more embeddings.

    The original query is always first in the returned list (we never
    drop it). On any failure path the function returns `[query]`, so
    the caller can iterate the result without special-casing the
    feature flag.
    """
    if not query or not query.strip():
        return []
    if num_variants <= 0:
        return [query]

    user_prompt = f"Query: {query}\n\nReturn {num_variants} variants."
    msgs = [AgentMessage(role="user", text=user_prompt)]

    sink = CallUsage()
    try:
        client = get_model_client()
        # Model precedence for this trivial emit-a-few-variants task:
        #   1. RAG_REWRITE_MODEL env — the operator escape hatch, and
        #      the per-subprocess rollback lever (no deploy needed).
        #   2. The effort pin (AGENT_EFFORT_LEVELS): the user's
        #      provider's `subprocesses.rewrite` rung — same-provider
        #      light model instead of silently inheriting the synthesis
        #      model (an Opus user was paying Opus rates here).
        #   3. None → inherit the synthesis model (legacy behavior).
        model_override = (
            settings.SEARCH_ENGINE.get("RAG_REWRITE_MODEL") or None
        ) or subprocess_model_override("rewrite", get_llm_choice())
        chunks: list[str] = []
        for text, fc in client.generate_step(
            messages=msgs,
            tools=[],
            system_instruction=_SYSTEM_PROMPT,
            model_override=model_override,
            usage_sink=sink,
            params=subprocess_params("rewrite"),
        ):
            if text:
                chunks.append(text)
            if fc is not None:
                log.warning(
                    "Query rewriter unexpectedly got a function call (name=%s) — ignoring",
                    fc.name,
                )
        raw = "".join(chunks).strip()
    except Exception:  # noqa: BLE001 — surface as fallback, not a crash
        log.exception("Query rewriter LLM call failed; using original query only")
        return [query]

    variants, kind = _parse_reply(raw)
    if not variants:
        # Its length and shape only: the variants restate the user's question (MIP-622).
        if kind == "empty_array":
            # Valid JSON with nothing in it (`[]`, or blank strings): the model had no
            # variant to offer, as for the query "a". The fallback is the designed
            # answer, so this is not a warning.
            log.info(
                "Query rewriter offered no variants (%d chars); using original query only",
                len(raw),
            )
        else:
            items = ""
            if kind == "non_string_items":
                found = _find_list(raw) or []
                items = f", items={_describe_items(found)}"
            log.warning(
                "Query rewriter returned unparseable output (%d chars, kind=%s%s, stop=%s%s); "
                "using original query only",
                len(raw),
                kind,
                items,
                sink.stop_reason or "n/a",
                ", truncated" if sink.truncated else "",
            )
        return [query]

    # Deduplicate (case-insensitive) while preserving order. The
    # original is always first so it wins any tie with a variant that
    # happens to repeat it.
    seen: set[str] = {query.strip().lower()}
    out: list[str] = [query]
    for v in variants[:num_variants]:
        key = v.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

# Match the first bracketed span, for the single-quote fallback below only.
_BRACKET_RE = re.compile(r"\[.*?\]", re.DOTALL)

# Bound on the `[` positions tried: replies are a few hundred characters, and a
# pathological one must not make the scan quadratic.
_MAX_ARRAY_STARTS = 20

_DECODER = json.JSONDecoder()


def _find_list(raw: str) -> list | None:
    """The first JSON array in `raw`, decoded from its opening `[` to its real end.

    Walking the text with `raw_decode` (not a `\\[.*?\\]` slice) keeps a `]` inside a
    string and a nested array whole, and skips bracketed prose ("[draft]") before the
    array. When no `[` starts valid JSON, a python-style list (single quotes, a trailing
    comma) is read as a literal: `ast.literal_eval` evaluates nothing.
    """
    pos = -1
    for _ in range(_MAX_ARRAY_STARTS):
        pos = raw.find("[", pos + 1)
        if pos < 0:
            break
        try:
            parsed, _end = _DECODER.raw_decode(raw, pos)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    match = _BRACKET_RE.search(raw)
    if match:
        try:
            parsed = ast.literal_eval(match.group(0))
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
        if isinstance(parsed, list):
            return parsed
    return None


def _parse_reply(raw: str) -> tuple[list[str], str]:
    """`(variants, kind)`. `kind` is "ok" or why there are none, so a log line can say
    which failure this is without printing the model's text:

      empty_reply       nothing came back
      no_array          no `[` at all (prose, a refusal, a bulleted list)
      bad_json          brackets, but not a list of any shape we read
      empty_array       a list with no usable string (`[]`, blank strings)
      non_string_items  a list whose items are not strings we can read (objects with
                        several strings, numbers; one string per object and one level
                        of nesting are read)
    """
    if not raw:
        return [], "empty_reply"
    if "[" not in raw:
        return [], "no_array"
    parsed = _find_list(raw)
    if parsed is None:
        return [], "bad_json"
    out: list[str] = []
    flat = [y for x in parsed for y in (x if isinstance(x, list) else [x])]
    for item in flat:
        text = _item_text(item)
        if text:
            out.append(text)
    if out:
        return out, "ok"
    if any(not isinstance(x, str) for x in parsed):
        return [], "non_string_items"
    return [], "empty_array"


def _item_text(item: object) -> str:
    """The variant an array item stands for, or "".

    A string is itself. An object that holds exactly one non-blank string
    (`{"query": "..."}`) is that string (a nested array is flattened one level
    by the caller). Anything else is not guessed at: the PR #875 CI run showed the model answering with
    non-string items in a few percent of the queries.
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        strings = [v.strip() for v in item.values() if isinstance(v, str) and v.strip()]
        return strings[0] if len(strings) == 1 else ""
    return ""


def _describe_items(parsed: list) -> str:
    """The types of a list's items, with no content: `dict(1 key)x2, int`."""
    counts: dict[str, int] = {}
    for x in parsed:
        name = type(x).__name__
        if isinstance(x, dict):
            name += f"({len(x)} key{'' if len(x) == 1 else 's'})"
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{k}x{n}" if n > 1 else k for k, n in sorted(counts.items()))


def _parse_variants(raw: str) -> list[str]:
    """Best-effort extraction of a JSON string array from the model output."""
    return _parse_reply(raw)[0]

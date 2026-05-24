"""LLM-as-judge — score a behavior case's answer along three axes.

Given a case's (query, retrieved sources, final answer), call a
separate LLM and ask it to score:

  * **faithfulness**: every factual claim in the answer is grounded
    in one of the provided source snippets.
  * **citation_precision**: each inline `[type:id]` citation
    actually supports the surrounding claim (not just topically
    related).
  * **completeness**: the answer covers the key information that
    the sources collectively contain about the query.

Each score is 0.0–1.0. The judge also returns a one-sentence note
explaining the lowest score.

Usage:

    from .judge import judge_answer
    scores = judge_answer(query=..., sources=[...], answer=...)
    # → {"faithfulness": 0.95, "citation_precision": 0.80,
    #    "completeness": 1.00, "notes": "..."}

Design choices
--------------
* Uses `get_model_client()` so the judge runs on whichever provider
  the host configured. For best results, configure
  `LLM_PROVIDER=claude` for the judge run (Claude tends to be a
  stricter grader than Gemini Flash on long-form quality questions).
* Judge prompt is **explicit about the rubric** and requires a
  strict JSON envelope so parsing is robust.
* Tolerates a model that wraps JSON in ```json fences — common
  failure mode that's cheap to defend against here vs. tightening
  the prompt.
* Sources are passed as a numbered list with title + snippet only —
  not full chunk text — to keep the judge's input bounded.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from origin.search_engine.llm import get_model_client
from origin.search_engine.llm.types import AgentMessage

log = logging.getLogger(__name__)

JUDGE_SYSTEM = """\
You are a strict evaluator scoring a Q&A system's output.

You will be given:
  - A user QUERY
  - A list of SOURCES (numbered; each has a title + short snippet)
  - A list of TOOL_RESULTS (the raw data the system saw — JSON
    dicts returned by tools like list_tasks, fetch_task,
    get_project_summary, etc.). For STRUCTURED tools, sources
    only carry the entity title; the real data (status, priority,
    due_date, counts, names) lives in TOOL_RESULTS. Treat
    TOOL_RESULTS as ground truth alongside SOURCES.
  - The system's ANSWER (markdown; may contain inline citations
    formatted as [task:N], [chat:dm:N:thread:N], [note:type:N], or
    [project:N]). Chat-type letters: `dm` = direct message,
    `gm` = group message (team channel), `pm` = project message
    (chat tied to a project — NOT "private message"), `mdm` = multi-DM.

Score three dimensions, each on a 0.0–1.0 scale:

  1. faithfulness — every factual claim in the ANSWER is supported
     by at least one SOURCE snippet OR by a field in TOOL_RESULTS.
     Unsupported claims = lower score (in proportion to how
     load-bearing the claim is). When the model quotes a status,
     priority, due_date, project name, or count, check TOOL_RESULTS
     first — those scalars rarely appear in source snippets.
     1.0 = every claim grounded. 0.0 = mostly hallucinated.

  2. citation_precision — each inline citation token in the ANSWER
     actually supports the surrounding sentence. Citing a source
     that's only topically adjacent = lower. Missing citations on
     load-bearing claims also count against this axis.
     **Aggregate/stats tools** (`get_workload_distribution`,
     `get_task_throughput_stats`, `get_stale_tasks` when empty,
     `get_project_activity_ranking` for non-project numbers) often
     produce user-level or aggregate numbers with no per-claim
     entity to cite. Do NOT penalize missing citations in that case
     — only flag missing citations when there is a clear entity in
     TOOL_RESULTS or SOURCES that the sentence is about.
     1.0 = every citation correctly attached. 0.0 = citations look
     random / wrong / absent on the key claims.

  3. completeness — the ANSWER covers the key information that the
     SOURCES + TOOL_RESULTS collectively contain about the QUERY.
     Missing a major point a source explicitly addresses = lower.
     1.0 = nothing important omitted. 0.0 = mostly missed the point.

Be strict — a 1.0 means you have no concerns. Most real answers
should land in 0.6–0.9 unless they're perfect.

Respond with a single JSON object, no prose, no markdown fences:

{
  "faithfulness": <0.0-1.0>,
  "citation_precision": <0.0-1.0>,
  "completeness": <0.0-1.0>,
  "notes": "one short sentence explaining the lowest score"
}
"""

# Match a fenced JSON block as a fallback parsing path. Some models
# wrap JSON in ```json … ``` despite the instruction not to.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def judge_answer(
    *,
    query: str,
    sources: list[dict[str, Any]],
    answer: str,
    tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score one (query, sources, answer) triple. Returns dict with
    `faithfulness`, `citation_precision`, `completeness`, and `notes`.

    `tool_results` is the list captured by the runner's `trace_hook` —
    each entry `{tool_name, arguments, result}`. Passing it lets the
    judge verify claims about structured-tool fields (status,
    priority, due_date, project_name, counts) that don't appear in
    `sources`. Omit to keep legacy behavior (sources-only judging).

    On any parsing or call failure returns `{"error": <reason>}` plus
    the same three score keys at 0.0 — caller can still aggregate.
    """
    user_prompt = _build_user_prompt(query, sources, answer, tool_results or [])
    client = get_model_client()

    try:
        chunks: list[str] = []
        for text, _fcall in client.generate_step(
            messages=[AgentMessage(role="user", text=user_prompt)],
            tools=[],
            system_instruction=JUDGE_SYSTEM,
        ):
            if text:
                chunks.append(text)
        raw = "".join(chunks).strip()
    except Exception as exc:  # noqa: BLE001 — never crash the eval suite
        log.warning("judge call failed: %s", exc, exc_info=True)
        return _error_scores(f"judge call failed: {exc!r}")

    parsed = _parse_judge_json(raw)
    if parsed is None:
        return _error_scores(f"judge returned non-JSON: {raw[:200]!r}")

    # Clamp to [0, 1] defensively — models occasionally drift.
    for k in ("faithfulness", "citation_precision", "completeness"):
        try:
            parsed[k] = max(0.0, min(1.0, float(parsed.get(k, 0))))
        except (TypeError, ValueError):
            parsed[k] = 0.0
    parsed.setdefault("notes", "")
    return parsed


def _build_user_prompt(
    query: str,
    sources: list[dict[str, Any]],
    answer: str,
    tool_results: list[dict[str, Any]],
) -> str:
    """Compose the user-side of the judge prompt.

    Sources are reduced to (n, title, snippet) so the judge sees the
    same surface a user sees in the chip row, not the full chunk
    body (which would balloon the prompt and isn't what citations
    point at semantically).

    Tool results are rendered as compact JSON with per-string-field
    truncation — the scalars (status / due_date / counts) are where
    hallucinations happen, so we keep those verbatim, but head/tail
    any long string field so a 100-message `fetch_chat_thread` or a
    task with 20 long comments doesn't blow up the judge prompt.
    """
    src_lines: list[str] = []
    for i, src in enumerate(sources, start=1):
        eid = src.get("entity_id") or "?"
        title = (src.get("title") or "").strip() or "(untitled)"
        snippet = (src.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "…"
        src_lines.append(f"  {i}. [{eid}] {title}\n     snippet: {snippet}")

    sources_block = "\n".join(src_lines) if src_lines else "  (no sources retrieved)"

    tool_lines: list[str] = []
    for i, tr in enumerate(tool_results, start=1):
        name = tr.get("tool_name") or "?"
        args = tr.get("arguments") or {}
        result = _truncate_for_judge(tr.get("result") or {})
        tool_lines.append(
            f"  {i}. {name}({json.dumps(args, ensure_ascii=False, default=str)})\n"
            f"     result: {json.dumps(result, ensure_ascii=False, default=str)}"
        )

    tools_block = "\n".join(tool_lines) if tool_lines else "  (no tool calls)"

    return (
        f"QUERY:\n  {query}\n\n"
        f"SOURCES:\n{sources_block}\n\n"
        f"TOOL_RESULTS:\n{tools_block}\n\n"
        f"ANSWER:\n{answer}\n"
    )


_MAX_STRING_LEN = 500
_MAX_LIST_LEN = 30


def _truncate_for_judge(value: Any) -> Any:
    """Recursively truncate long strings and long lists for the judge
    prompt. Scalars (numbers, bools, dates) pass through verbatim —
    those are exactly the fields the judge needs to verify, and
    truncating them would defeat the purpose.
    """
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LEN:
            head = value[: _MAX_STRING_LEN // 2]
            tail = value[-_MAX_STRING_LEN // 2 :]
            return f"{head} … [{len(value) - _MAX_STRING_LEN} chars elided] … {tail}"
        return value
    if isinstance(value, list):
        truncated = [_truncate_for_judge(v) for v in value[:_MAX_LIST_LEN]]
        if len(value) > _MAX_LIST_LEN:
            truncated.append(f"… [{len(value) - _MAX_LIST_LEN} more items elided]")
        return truncated
    if isinstance(value, dict):
        return {k: _truncate_for_judge(v) for k, v in value.items()}
    return value


def _parse_judge_json(raw: str) -> dict[str, Any] | None:
    """Parse the judge's reply. Tolerates an optional ```json fence."""
    raw = raw.strip()
    if not raw:
        return None

    # Fast path — the prompt asks for bare JSON.
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    # Fallback — strip a ```json … ``` fence.
    m = _FENCE_RE.search(raw)
    if m:
        try:
            loaded = json.loads(m.group(1))
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            return None
    return None


def _error_scores(reason: str) -> dict[str, Any]:
    return {
        "faithfulness": 0.0,
        "citation_precision": 0.0,
        "completeness": 0.0,
        "notes": "",
        "error": reason,
    }

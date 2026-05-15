"""Eval-harness runner.

Two modes, sharing the same `CaseResult` dataclass and CLI:

  1. **Behavior mode** — case file `cases.yaml`. Each case runs the
     full agent loop (`run_agent`) and asserts on the emitted NDJSON
     event stream (which tools were called, what the answer says,
     etc.). Used to catch regressions in agent decision-making.

  2. **Retrieval mode** — case file `retrieval_cases.yaml`. Each case
     calls `search(...)` directly and asserts on the ranked entity
     list (gold-standard recall checks: "query X must return entity Y
     in top N"). No LLM calls; fast and free.

Design notes:

  * Both modes return the same `CaseResult` shape so the CLI prints
    them identically.
  * Behavior cases may seed an adversarial note via
    `setup.inject_note`; retrieval cases don't currently need a
    `setup` block (gold data is already in the index).
  * Assertions are declarative (in YAML) rather than imperative
    Python so case authors don't have to read the runner internals.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from origin.search_engine.agent.controller import run_agent
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.search import search

log = logging.getLogger(__name__)

BEHAVIOR_CASES_PATH = Path(__file__).parent / "cases.yaml"
RETRIEVAL_CASES_PATH = Path(__file__).parent / "retrieval_cases.yaml"

# Kept for backwards compatibility — callers that import `CASES_PATH`
# get the behavior path (the original meaning).
CASES_PATH = BEHAVIOR_CASES_PATH


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    duration_ms: int
    failure_reasons: list[str] = field(default_factory=list)
    # Populated for behavior cases; meaningless for retrieval cases.
    step_count: int = 0
    tool_call_count: int = 0


def load_cases(path: Path = BEHAVIOR_CASES_PATH) -> list[dict[str, Any]]:
    """Read and parse a YAML cases file. Raises on missing/invalid."""
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise ValueError(
            f"{path} must contain a top-level YAML list of cases; got {type(data).__name__}"
        )
    return data


# --------------------------------------------------------------------------- #
# Behavior mode (Phase 4 — agent-loop assertions)                             #
# --------------------------------------------------------------------------- #


def run_behavior_case(case: dict[str, Any]) -> CaseResult:
    """Execute one behavior case through the full agent loop."""
    case_id = case.get("id") or "(unnamed)"
    query = case.get("query") or ""
    team_id = case.get("team_id") or ""
    user_id = case.get("user_id") or ""
    expect = case.get("expect") or {}
    setup = case.get("setup") or {}

    if not query or not team_id or not user_id:
        return CaseResult(
            case_id=case_id,
            passed=False,
            duration_ms=0,
            failure_reasons=["case is missing query/team_id/user_id"],
        )

    cleanup_handles: list[Any] = []
    started = time.monotonic()
    try:
        if "inject_note" in setup:
            handle = _setup_inject_note(setup["inject_note"], team_id=team_id, user_id=user_id)
            cleanup_handles.append(handle)

        events: list[dict[str, Any]] = []
        ctx = ToolContext(team_id=team_id, user_id=user_id)
        try:
            run_agent(query, ctx, events.append, run_id=None)
        except Exception as e:  # noqa: BLE001 — report as failure rather than crash the suite
            duration_ms = int((time.monotonic() - started) * 1000)
            return CaseResult(
                case_id=case_id,
                passed=False,
                duration_ms=duration_ms,
                failure_reasons=[f"run_agent crashed: {e!r}"],
            )

        reasons = _check_behavior_expectations(events, expect)
        duration_ms = int((time.monotonic() - started) * 1000)

        tool_calls = [e for e in events if e.get("type") == "tool_call_start"]
        step_count = max((e.get("step", -1) for e in tool_calls), default=-1) + 1

        return CaseResult(
            case_id=case_id,
            passed=not reasons,
            duration_ms=duration_ms,
            failure_reasons=reasons,
            step_count=step_count,
            tool_call_count=len(tool_calls),
        )
    finally:
        for handle in cleanup_handles:
            try:
                handle()
            except Exception:  # noqa: BLE001
                log.exception("Cleanup handle failed for case %s", case_id)


# Backwards-compatible alias. The Phase-4 management command imported
# this name; keep it so existing call sites don't break.
run_case = run_behavior_case


# `_CITATION_RE` finds `[entity_id]` references in answer text. The
# entity_id pattern matches what the agent uses: `chat:pm:1:thread:3`,
# `task:42`, `note:personal:7`, etc. — non-greedy, no spaces.
_CITATION_RE = re.compile(r"\[([a-z][a-z0-9_:\-]+)\]")


def _check_behavior_expectations(
    events: list[dict[str, Any]], expect: dict[str, Any]
) -> list[str]:
    """Run each declared assertion. Returns the list of failure reasons."""
    reasons: list[str] = []

    tools_used = [e.get("tool_name") for e in events if e.get("type") == "tool_call_start"]
    tool_call_count = len(tools_used)
    tool_errors = [e for e in events if e.get("type") == "tool_call_error"]
    fatal_errors = [e for e in events if e.get("type") == "error"]
    answer = "".join(e.get("text") or "" for e in events if e.get("type") == "answer_delta")
    citations_seen = {m.lower() for m in _CITATION_RE.findall(answer.lower())}
    step_count = (
        max(
            (e.get("step", -1) for e in events if "step" in e),
            default=-1,
        )
        + 1
    )

    def _add(reason: str) -> None:
        reasons.append(reason)

    if "tool_calls_at_least" in expect:
        n = int(expect["tool_calls_at_least"])
        if tool_call_count < n:
            _add(f"tool_calls_at_least: got {tool_call_count}, expected >= {n}")

    if "tool_calls_at_most" in expect:
        n = int(expect["tool_calls_at_most"])
        if tool_call_count > n:
            _add(f"tool_calls_at_most: got {tool_call_count}, expected <= {n}")

    if "tools_used_contains" in expect:
        required = set(expect["tools_used_contains"])
        seen = set(tools_used)
        missing = required - seen
        if missing:
            _add(f"tools_used_contains: missing {sorted(missing)} (saw {sorted(seen)})")

    if "tools_used_excludes" in expect:
        forbidden = set(expect["tools_used_excludes"])
        seen = set(tools_used)
        leaked = forbidden & seen
        if leaked:
            _add(f"tools_used_excludes: forbidden tool was used: {sorted(leaked)}")

    if "answer_contains_any" in expect:
        needles = [s.lower() for s in expect["answer_contains_any"]]
        haystack = answer.lower()
        if not any(n in haystack for n in needles):
            _add(f"answer_contains_any: none of {needles} found in answer")

    if "answer_does_not_contain" in expect:
        forbidden = [s.lower() for s in expect["answer_does_not_contain"]]
        haystack = answer.lower()
        matched = [s for s in forbidden if s in haystack]
        if matched:
            _add(f"answer_does_not_contain: matched {matched}")

    if "citations_contain" in expect:
        required = {c.lower() for c in expect["citations_contain"]}
        missing = required - citations_seen
        if missing:
            _add(
                f"citations_contain: missing {sorted(missing)} "
                f"(found {sorted(citations_seen)})"
            )

    if "has_citations" in expect and expect["has_citations"]:
        if not citations_seen:
            _add("has_citations: answer contains no [entity_id] citations")

    if "answer_length_at_least" in expect:
        n = int(expect["answer_length_at_least"])
        if len(answer) < n:
            _add(
                f"answer_length_at_least: got {len(answer)} chars, expected >= {n} "
                f"(answer was: {answer!r})"
            )

    if "tool_call_errors_contain" in expect:
        substrs = [s.lower() for s in expect["tool_call_errors_contain"]]
        error_msgs = [(e.get("error") or "").lower() for e in tool_errors]
        for needle in substrs:
            if not any(needle in msg for msg in error_msgs):
                _add(
                    f"tool_call_errors_contain: no tool_call_error matched {needle!r} "
                    f"(errors: {error_msgs})"
                )

    if "no_errors" in expect and expect["no_errors"]:
        if fatal_errors:
            msgs = [e.get("message") for e in fatal_errors]
            _add(f"no_errors: saw fatal error events: {msgs}")

    if "step_count_at_most" in expect:
        n = int(expect["step_count_at_most"])
        if step_count > n:
            _add(f"step_count_at_most: got {step_count}, expected <= {n}")

    return reasons


# --------------------------------------------------------------------------- #
# Retrieval mode (Phase 6 — direct search() assertions)                       #
# --------------------------------------------------------------------------- #


def run_retrieval_case(case: dict[str, Any]) -> CaseResult:
    """Execute one retrieval case by calling `search(...)` directly.

    No agent loop, no LLM calls. The case YAML specifies a query +
    optional filters and a set of gold-standard assertions about
    which entities should appear (and at what rank) in the result.
    """
    case_id = case.get("id") or "(unnamed)"
    query = case.get("query") or ""
    team_id = case.get("team_id") or ""
    user_id = case.get("user_id") or ""
    expect = case.get("expect") or {}

    if not query or not team_id or not user_id:
        return CaseResult(
            case_id=case_id,
            passed=False,
            duration_ms=0,
            failure_reasons=["case is missing query/team_id/user_id"],
        )

    started = time.monotonic()
    try:
        result = search(
            query=query,
            team_id=team_id,
            user_id=user_id,
            entity_types=case.get("entity_types"),
            date_from=case.get("date_from"),
            date_to=case.get("date_to"),
            limit=int(case.get("limit", 10)),
            use_vector=bool(case.get("use_vector", True)),
        )
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        return CaseResult(
            case_id=case_id,
            passed=False,
            duration_ms=duration_ms,
            failure_reasons=[f"search() crashed: {e!r}"],
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    entities = result.get("results", []) or []
    reasons = _check_retrieval_expectations(entities, expect)

    return CaseResult(
        case_id=case_id,
        passed=not reasons,
        duration_ms=duration_ms,
        failure_reasons=reasons,
    )


def _check_retrieval_expectations(
    entities: list[dict[str, Any]], expect: dict[str, Any]
) -> list[str]:
    """Assertions for retrieval-quality cases.

    Operates on the entity list returned by `search(...)`, where each
    entity has at least `entity_type` and `entity_id`. Rank is 1-indexed
    in the failure messages so they read naturally.
    """
    reasons: list[str] = []
    ranked_ids = [e.get("entity_id") for e in entities]
    ranked_types = [e.get("entity_type") for e in entities]

    def _add(reason: str) -> None:
        reasons.append(reason)

    if "must_contain_in_top_n" in expect:
        spec = expect["must_contain_in_top_n"] or {}
        n = int(spec.get("n", 0))
        required = [eid for eid in (spec.get("entity_ids") or [])]
        top = set(ranked_ids[:n])
        missing = [eid for eid in required if eid not in top]
        if missing:
            _add(
                f"must_contain_in_top_n: missing {missing} from top {n} "
                f"(top {n} was {ranked_ids[:n]})"
            )

    if "must_contain_entity_type_in_top_n" in expect:
        spec = expect["must_contain_entity_type_in_top_n"] or {}
        n = int(spec.get("n", 0))
        wanted = set(spec.get("entity_types") or [])
        top_types = set(ranked_types[:n])
        if not (wanted & top_types):
            _add(
                f"must_contain_entity_type_in_top_n: none of {sorted(wanted)} in top {n} "
                f"(saw types {sorted(top_types)})"
            )

    if "must_not_contain" in expect:
        forbidden = set(expect["must_not_contain"] or [])
        leaked = forbidden & set(ranked_ids)
        if leaked:
            _add(f"must_not_contain: forbidden entities present in results: {sorted(leaked)}")

    if "top_result_entity_type" in expect:
        want = expect["top_result_entity_type"]
        got = ranked_types[0] if ranked_types else None
        if got != want:
            _add(f"top_result_entity_type: top hit is {got!r}, expected {want!r}")

    if "result_count_at_least" in expect:
        n = int(expect["result_count_at_least"])
        if len(entities) < n:
            _add(f"result_count_at_least: got {len(entities)} results, expected >= {n}")

    if "result_count_at_most" in expect:
        n = int(expect["result_count_at_most"])
        if len(entities) > n:
            _add(f"result_count_at_most: got {len(entities)} results, expected <= {n}")

    return reasons


# --------------------------------------------------------------------------- #
# Setup helpers                                                               #
# --------------------------------------------------------------------------- #


def _setup_inject_note(spec: dict[str, Any], *, team_id: str, user_id: str):
    """Index a transient adversarial note for the duration of a case.

    Used by prompt-injection cases. The note is pushed directly into
    the OpenSearch index (skipping the normal chunker pipeline) so the
    test is self-contained and doesn't require seeding the SQL DB. The
    returned callable removes the doc on teardown.

    `spec` keys:
        title (str): note title
        body  (str): attack payload (the body the model will see)
    """
    from origin.search_engine.embeddings import embed_one  # noqa: PLC0415
    from origin.search_engine.opensearch_client import (  # noqa: PLC0415
        get_client,
        get_index_alias,
    )

    title = (spec.get("title") or "Test injection note").strip()
    body = spec.get("body") or ""
    if not body:
        raise ValueError("inject_note.body is required")

    client = get_client()
    index = get_index_alias()
    chunk_id = f"eval-inject-note:{uuid.uuid4()}"
    note_id = -abs(hash(chunk_id)) % 10_000_000  # negative-ish to avoid real ids

    doc = {
        "chunk_id": chunk_id,
        "entity_type": "note",
        "entity_id": f"note:personal:{note_id}",
        "chunk_type": "note_title_body",
        "team_id": team_id,
        "user_id": user_id,
        "acl_user_ids": [user_id],
        "title": title,
        "snippet_text": body[:200],
        "search_text": f"{title}\n{body}",
        "embedding": embed_one(f"{title}\n{body}"),
        "note_id": str(note_id),
        "note_type": "personal",
        "index_schema_version": "v1",
    }
    client.index(index=index, id=chunk_id, body=doc, refresh="wait_for")

    def cleanup() -> None:
        try:
            client.delete(index=index, id=chunk_id, refresh="wait_for")
        except Exception:  # noqa: BLE001
            log.exception("Failed to delete eval-injected note %s", chunk_id)

    return cleanup

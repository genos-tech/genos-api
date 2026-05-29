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


def _resolve_fixture(case: dict[str, Any]) -> dict[str, Any]:
    """If `case.fixture == True`, fill in team_id / user_id from the
    deterministic eval fixture (see `agent/evals/fixture.py`).

    Mutates and returns the same dict for convenience. Cases that
    pin their own team_id are left untouched — useful for legacy
    dev-DB fixture cases and adversarial cross-tenant tests.
    """
    if not case.get("fixture"):
        return case

    # Lazy import — the fixture module touches Django models, which
    # would blow up if imported at module load before app-ready.
    from origin.search_engine.agent.evals.fixture import (  # noqa: PLC0415
        FIXTURE_USER_ID,
        ensure_fixture,
    )

    info = ensure_fixture()
    case.setdefault("team_id", info["team_id"])
    case.setdefault("user_id", str(FIXTURE_USER_ID))
    return case


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    duration_ms: int
    failure_reasons: list[str] = field(default_factory=list)
    # Populated for behavior cases; meaningless for retrieval cases.
    step_count: int = 0
    tool_call_count: int = 0
    # Captured behavior-case artefacts — populated only when the caller
    # asked for them (e.g. `judge=True`). Kept on the result dataclass
    # so the LLM judge / trace writer can read them without re-running.
    query: str = ""
    answer: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    # Full tool-call traces captured via the controller's `trace_hook`.
    # Each entry: {"tool_name": str, "arguments": dict, "result": dict}.
    # Used by the LLM judge to verify the answer's factual claims
    # against the actual data the model saw (sources alone are too
    # sparse for structured-tool answers — they carry only entity_id
    # and title, not the status/due_date/priority the model legitimately
    # quotes from a `list_tasks` result).
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # Phase 4.4 — milliseconds from run_agent invocation to the first
    # `answer_delta` event (the strict TTFT metric). -1 if no
    # answer_delta was ever emitted (model errored or returned no text).
    ttft_ms: int = -1
    # Optional LLM-judge scores; only set when `--judge` was on.
    judge_scores: dict[str, Any] | None = None
    # Continuous quality metrics layered on top of the binary pass/fail
    # (Q0 of SPOTLIGHT_QUALITY_ARCHITECTURE.md). Retrieval cases populate
    # rank-based signals (`mrr`, `recall_at_n`); behavior cases leave it
    # empty today (see `_retrieval_metrics` for why tool-selection is not
    # yet a continuous metric on this suite). Empty `{}` → no metric for
    # this case, so aggregators skip it.
    metrics: dict[str, float] = field(default_factory=dict)


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
    """Execute one behavior case through the full agent loop.

    Single-turn shape (default):
        - id: ...
          query: "..."
          expect: {...}

    Multi-turn shape (Phase 3.5):
        - id: ...
          turns:
            - query: "first turn"           # no expect
            - query: "second turn"          # no expect
            - query: "final turn"
              expect: {...}                  # assertions on the final turn

        Assertions live on the LAST turn (or in the case's top-level
        `expect` block). The runner threads `prior_turns` between turns
        the same way `agent_views.py` does in production, so the case
        exercises real multi-turn memory.
    """
    case = _resolve_fixture(case)
    case_id = case.get("id") or "(unnamed)"

    # Multi-turn fork — handled in a dedicated helper that mirrors the
    # single-turn flow per-turn and thread prior turns between them.
    if "turns" in case:
        return _run_multiturn_case(case, case_id)

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
        tool_traces: list[dict[str, Any]] = []
        # Phase 4.4 — capture per-event timing to compute TTFT.
        # Wrapping the emit callback is the smallest non-invasive hook
        # (controller code untouched). Timestamps are relative to the
        # run_agent invocation, in seconds.
        emit_t0 = time.monotonic()
        ttft_s: float | None = None

        def _ts_emit(event: dict[str, Any]) -> None:
            nonlocal ttft_s
            if (
                ttft_s is None
                and event.get("type") == "answer_delta"
                and (event.get("text") or "")
            ):
                ttft_s = time.monotonic() - emit_t0
            events.append(event)

        def _capture_trace(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
            tool_traces.append({"tool_name": name, "arguments": args, "result": result})

        ctx = ToolContext(team_id=team_id, user_id=user_id)
        try:
            run_agent(query, ctx, _ts_emit, run_id=None, trace_hook=_capture_trace)
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

        # Capture answer + last `sources` snapshot so the LLM judge
        # (or any post-hoc analyser) can score this run without
        # re-executing it.
        answer_text = "".join(
            e.get("text") or "" for e in events if e.get("type") == "answer_delta"
        )
        source_events = [e for e in events if e.get("type") == "sources"]
        last_sources = source_events[-1].get("sources", []) if source_events else []

        return CaseResult(
            case_id=case_id,
            passed=not reasons,
            duration_ms=duration_ms,
            failure_reasons=reasons,
            step_count=step_count,
            tool_call_count=len(tool_calls),
            query=query,
            answer=answer_text,
            sources=list(last_sources),
            tool_results=tool_traces,
            ttft_ms=int(ttft_s * 1000) if ttft_s is not None else -1,
            metrics=_tool_selection_metrics(events, expect),
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


def _run_multiturn_case(case: dict[str, Any], case_id: str) -> CaseResult:
    """Phase 3.5 — execute a multi-turn case by running the agent loop
    once per turn, threading prior (query, answer) pairs between turns.

    Mirrors the production agent_views.py session-threading shape so the
    multi-turn-memory mechanism is exercised end-to-end. The eval does
    NOT honor `SESSION_MAX_PRIOR_TURNS` truncation itself — it passes
    ALL prior turns to `run_agent`, the same way production would for a
    fresh session. Truncation/summarisation is applied INSIDE the
    prior-turns prep helper, so flag-toggling `RAG_SESSION_ROLLING_SUMMARY`
    in an A/B works without changing the case shape.

    Assertions live on the LAST turn's expectations (or on the case's
    top-level `expect`). Earlier turns are setup; we never fail-fast on
    a per-turn intermediate.
    """
    turns = case.get("turns") or []
    if not turns:
        return CaseResult(
            case_id=case_id,
            passed=False,
            duration_ms=0,
            failure_reasons=["multi-turn case has empty `turns` list"],
        )

    team_id = case.get("team_id") or ""
    user_id = case.get("user_id") or ""
    if not team_id or not user_id:
        return CaseResult(
            case_id=case_id,
            passed=False,
            duration_ms=0,
            failure_reasons=["case is missing team_id/user_id"],
        )

    # The terminal expectation set: prefer the last turn's `expect`,
    # else fall back to the case-level `expect`.
    final_turn = turns[-1]
    final_expect = final_turn.get("expect") or case.get("expect") or {}

    # Phase 3.5 — defer truncation + (optional) rolling summary to the
    # shared helper, so a single `RAG_SESSION_ROLLING_SUMMARY=true`
    # override in agent_eval_compare exercises the exact code path
    # production /ask/ uses. Helper is no-op when the flag is off OR
    # the session is shorter than the verbatim window — i.e. matches
    # pre-3.5 behavior unless explicitly opted in.
    from origin.search_engine.agent.multi_turn import build_prior_context  # noqa: PLC0415

    ctx = ToolContext(team_id=team_id, user_id=user_id)
    prior_turns: list[tuple[str, str]] = []
    last_events: list[dict[str, Any]] = []
    last_tool_traces: list[dict[str, Any]] = []
    last_query = ""
    last_ttft_s: float | None = None
    started = time.monotonic()

    for i, turn in enumerate(turns):
        q = (turn.get("query") or "").strip()
        if not q:
            return CaseResult(
                case_id=case_id,
                passed=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_reasons=[f"turn {i + 1} is missing a non-empty `query`"],
            )

        events: list[dict[str, Any]] = []
        tool_traces: list[dict[str, Any]] = []
        # Per-turn TTFT — we only retain the FINAL turn's value below.
        per_turn_t0 = time.monotonic()
        per_turn_ttft_s: float | None = None

        def _ts_emit(event: dict[str, Any]) -> None:
            nonlocal per_turn_ttft_s
            if (
                per_turn_ttft_s is None
                and event.get("type") == "answer_delta"
                and (event.get("text") or "")
            ):
                per_turn_ttft_s = time.monotonic() - per_turn_t0
            events.append(event)

        def _capture_trace(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
            tool_traces.append({"tool_name": name, "arguments": args, "result": result})

        verbatim_turns, summary = build_prior_context(prior_turns)
        try:
            run_agent(
                q,
                ctx,
                _ts_emit,
                run_id=None,
                prior_turns=verbatim_turns,
                prior_summary=summary,
                trace_hook=_capture_trace,
            )
        except Exception as e:  # noqa: BLE001
            return CaseResult(
                case_id=case_id,
                passed=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                failure_reasons=[f"turn {i + 1} run_agent crashed: {e!r}"],
            )

        answer_text = "".join(
            (e.get("text") or "") for e in events if e.get("type") == "answer_delta"
        )
        prior_turns.append((q, answer_text))
        last_query = q
        last_events = events
        last_tool_traces = tool_traces
        last_ttft_s = per_turn_ttft_s

    # Score only the final turn.
    reasons = _check_behavior_expectations(last_events, final_expect)
    duration_ms = int((time.monotonic() - started) * 1000)

    tool_calls = [e for e in last_events if e.get("type") == "tool_call_start"]
    step_count = max((e.get("step", -1) for e in tool_calls), default=-1) + 1
    answer_text = "".join(
        (e.get("text") or "") for e in last_events if e.get("type") == "answer_delta"
    )
    source_events = [e for e in last_events if e.get("type") == "sources"]
    last_sources = source_events[-1].get("sources", []) if source_events else []

    return CaseResult(
        case_id=case_id,
        passed=not reasons,
        duration_ms=duration_ms,
        failure_reasons=reasons,
        step_count=step_count,
        tool_call_count=len(tool_calls),
        query=last_query,
        answer=answer_text,
        sources=list(last_sources),
        tool_results=last_tool_traces,
        ttft_ms=int(last_ttft_s * 1000) if last_ttft_s is not None else -1,
        metrics=_tool_selection_metrics(last_events, final_expect),
    )


# `_CITATION_RE` finds `[entity_id]` references in answer text. The
# entity_id pattern matches what the agent uses: `chat:pm:1:thread:3`,
# `task:42`, `note:personal:7`, etc. — non-greedy, no spaces.
_CITATION_RE = re.compile(r"\[([a-z][a-z0-9_:\-]+)\]")


def _tool_selection_metrics(
    events: list[dict[str, Any]], expect: dict[str, Any]
) -> dict[str, float]:
    """Continuous tool-selection signals (Q0) layered on the binary tool
    assertions.

    Reads ONLY the metric-only gold fields — `expected_tools` and
    `forbidden_tools` — never the gating `tools_used_contains` /
    `tools_used_excludes`. That's deliberate: a gating assertion is
    structurally pinned (a *passing* `tools_used_contains` case always has
    recall 1.0; a passing `tools_used_excludes` case always has excl_ok
    1.0), so reading it would inject constant 1.0s that swamp the real
    signal — the same binary-in-a-costume trap as recall@n on singleton
    gold. The metric-only fields can actually move:

      * tool_recall  = |expected_tools ∩ used| / |expected_tools|
        Fractional when the model takes a one-tool shortcut on a
        genuinely multi-tool question.
      * tool_excl_ok = |forbidden_tools \\ used| / |forbidden_tools|
        Drops when the agent over-reaches to a tool it shouldn't (e.g.
        paid web search on an internal question).

    Both are NON-gating — the case's pass/fail comes from its other
    assertions — so path-sensitive multi-tool / negative gold can't
    flaky-fail the CI gate. See the `expected_tools` / `forbidden_tools`
    cases in cases.yaml. Returns `{}` when neither field is declared.
    """
    used = {e.get("tool_name") for e in events if e.get("type") == "tool_call_start"}
    out: dict[str, float] = {}
    expected = expect.get("expected_tools") or []
    if expected:
        req = set(expected)
        out["tool_recall"] = round(len(req & used) / len(req), 4)
    forbidden = expect.get("forbidden_tools") or []
    if forbidden:
        forb = set(forbidden)
        out["tool_excl_ok"] = round(len(forb - used) / len(forb), 4)
    return out


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

    if "no_citations" in expect and expect["no_citations"]:
        if citations_seen:
            _add(f"no_citations: answer contains citations: {sorted(citations_seen)}")

    if "citations_count_at_least" in expect:
        n = int(expect["citations_count_at_least"])
        if len(citations_seen) < n:
            _add(
                f"citations_count_at_least: got {len(citations_seen)} citation(s), "
                f"expected >= {n} (saw {sorted(citations_seen)})"
            )

    if "answer_contains_all" in expect:
        needles = [s.lower() for s in expect["answer_contains_all"]]
        haystack = answer.lower()
        missing = [n for n in needles if n not in haystack]
        if missing:
            _add(f"answer_contains_all: missing {missing} from answer")

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


def _retrieval_metrics(entities: list[dict[str, Any]], expect: dict[str, Any]) -> dict[str, float]:
    """Continuous retrieval-quality signals on top of the binary pass/fail.

    Why MRR (rank) is the headline rather than recall@n: most gold sets in
    `retrieval_cases.yaml` are singletons (one entity / one title
    substring), so a recall@n fraction is just 0.0/1.0 — identical to the
    existing `must_contain_*` binary, trending nothing new. The *rank* at
    which the gold item lands distinguishes "surfaced at #1" from
    "surfaced at #5" — both pass the binary today but are very different
    retrieval outcomes, and a retrieval change that lifts gold from rank 4
    to rank 2 is invisible to pass/fail. `recall_at_n` is still reported
    but is only fractional for the handful of multi-gold cases.

    Scope (do not overclaim): retrieval cases run under `mode="eval"`
    (freshness + chunk-type overlays OFF — see `run_retrieval_case`), so
    these measure RAW BM25+vector+RRF recall on fixtures. They are ideal
    for A/B-ing a retrieval change, but are NOT production recall — that
    is the online-sampling half of the foundation, which this doesn't
    touch. Ranks are measured within the returned list (capped at the
    case's `limit`); gold outside it scores reciprocal rank 0.

    Tool-selection accuracy is intentionally NOT emitted here: every
    `tools_used_contains` in `cases.yaml` is a singleton, so a "tool
    recall" number would be purely binary (a fraction in a binary
    costume). A continuous tool-selection metric is blocked on authoring
    multi-tool / negative-tool gold cases first.

    Returns `{}` for cases with no rank-checkable gold (e.g. a pure
    `must_not_contain_title` adversarial case) so the caller skips them.
    """
    ranked_titles = [(e.get("title") or "").lower() for e in entities]
    ranked_ids = [e.get("entity_id") for e in entities]

    def _rank_of_title(needle: str) -> int | None:
        needle = (needle or "").lower()
        if not needle:
            return None
        for i, t in enumerate(ranked_titles):
            if needle in t:
                return i + 1  # 1-indexed
        return None

    def _rank_of_id(eid: Any) -> int | None:
        for i, x in enumerate(ranked_ids):
            if x == eid:
                return i + 1
        return None

    # Each gold "slot" contributes (reciprocal_rank, hit_within_declared_n).
    slots: list[tuple[float, bool]] = []

    needle = expect.get("top_result_title_contains")
    if isinstance(needle, str) and needle:
        r = _rank_of_title(needle)
        slots.append((1.0 / r if r else 0.0, bool(r and r <= 1)))

    spec = expect.get("must_contain_title_in_top_n")
    if isinstance(spec, dict):
        n = int(spec.get("n", 0))
        for s in spec.get("title_substrings") or []:
            r = _rank_of_title(s)
            slots.append((1.0 / r if r else 0.0, bool(r and r <= n)))

    spec = expect.get("must_contain_in_top_n")
    if isinstance(spec, dict):
        n = int(spec.get("n", 0))
        for eid in spec.get("entity_ids") or []:
            r = _rank_of_id(eid)
            slots.append((1.0 / r if r else 0.0, bool(r and r <= n)))

    # OR matcher: one slot, satisfied by the best-ranked candidate.
    spec = expect.get("must_contain_any_title_in_top_n")
    if isinstance(spec, dict):
        n = int(spec.get("n", 0))
        ranks = [r for r in (_rank_of_title(s) for s in spec.get("title_substrings") or []) if r]
        best = min(ranks) if ranks else None
        slots.append((1.0 / best if best else 0.0, bool(best and best <= n)))

    if not slots:
        return {}
    mrr = sum(rr for rr, _ in slots) / len(slots)
    recall = sum(1.0 for _, hit in slots if hit) / len(slots)
    return {"mrr": round(mrr, 4), "recall_at_n": round(recall, 4)}


def run_retrieval_case(case: dict[str, Any]) -> CaseResult:
    """Execute one retrieval case by calling `search(...)` directly.

    No agent loop, no LLM calls. The case YAML specifies a query +
    optional filters and a set of gold-standard assertions about
    which entities should appear (and at what rank) in the result.
    """
    case = _resolve_fixture(case)
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
        # Honor the same RAG_USE_QUERY_REWRITE flag the agent path
        # uses, so `agent_eval_compare --b-overrides
        # '{"RAG_USE_QUERY_REWRITE": true}'` actually exercises
        # rewriting on the retrieval suite. Lazy import — `settings`
        # is set up once Django has loaded.
        from django.conf import settings as _settings  # noqa: PLC0415

        result = search(
            query=query,
            team_id=team_id,
            user_id=user_id,
            entity_types=case.get("entity_types"),
            date_from=case.get("date_from"),
            date_to=case.get("date_to"),
            limit=int(case.get("limit", 10)),
            use_vector=bool(case.get("use_vector", True)),
            rewrite=bool(_settings.SEARCH_ENGINE.get("RAG_USE_QUERY_REWRITE", False)),
            # `mode="eval"` disables freshness boost + chunk-type
            # reweighting so the retrieval-quality numbers reflect raw
            # BM25 + vector + RRF, not the production-tuned overlays.
            mode="eval",
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
        metrics=_retrieval_metrics(entities, expect),
    )


def _check_retrieval_expectations(
    entities: list[dict[str, Any]], expect: dict[str, Any]
) -> list[str]:
    """Assertions for retrieval-quality cases.

    Operates on the entity list returned by `search(...)`, where each
    entity has at least `entity_type`, `entity_id`, and `title`. Rank
    is 1-indexed in the failure messages so they read naturally.
    """
    reasons: list[str] = []
    ranked_ids = [e.get("entity_id") for e in entities]
    ranked_types = [e.get("entity_type") for e in entities]
    ranked_titles = [(e.get("title") or "") for e in entities]

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

    # Title-substring AND matcher — every entry in `title_substrings`
    # must appear (case-insensitive) in the title of some entity in
    # the top N. Use when ALL of several expected entities need to
    # surface together. Robust across reseedings.
    if "must_contain_title_in_top_n" in expect:
        spec = expect["must_contain_title_in_top_n"] or {}
        n = int(spec.get("n", 0))
        required = [s.lower() for s in (spec.get("title_substrings") or [])]
        top_titles = [t.lower() for t in ranked_titles[:n]]
        missing = [needle for needle in required if not any(needle in t for t in top_titles)]
        if missing:
            _add(
                f"must_contain_title_in_top_n: missing {missing} from top {n} "
                f"(top {n} titles were {ranked_titles[:n]})"
            )

    # Title-substring OR matcher — AT LEAST ONE entry in
    # `title_substrings` must appear in the top N. Use when the
    # question has multiple acceptable answers and any of them
    # surfacing is a pass.
    if "must_contain_any_title_in_top_n" in expect:
        spec = expect["must_contain_any_title_in_top_n"] or {}
        n = int(spec.get("n", 0))
        candidates = [s.lower() for s in (spec.get("title_substrings") or [])]
        top_titles = [t.lower() for t in ranked_titles[:n]]
        any_match = any(needle in t for needle in candidates for t in top_titles)
        if candidates and not any_match:
            _add(
                f"must_contain_any_title_in_top_n: none of {candidates} found in top {n} "
                f"(top {n} titles were {ranked_titles[:n]})"
            )

    # Title-substring NEGATIVE matcher — none of these substrings may
    # appear as a title in the result set. Use for adversarial /
    # ACL-leak cases ("the off-team document must NOT surface").
    if "must_not_contain_title" in expect:
        forbidden = [s.lower() for s in (expect["must_not_contain_title"] or [])]
        leaked = [
            needle for needle in forbidden if any(needle in t.lower() for t in ranked_titles)
        ]
        if leaked:
            _add(
                f"must_not_contain_title: forbidden title substring(s) "
                f"appeared: {leaked} (titles: {ranked_titles})"
            )

    # Top-result title matcher — the #1 ranked entity must have a
    # title that contains this substring (case-insensitive).
    if "top_result_title_contains" in expect:
        needle = str(expect["top_result_title_contains"]).lower()
        top_title = ranked_titles[0].lower() if ranked_titles else ""
        if needle not in top_title:
            _add(
                f"top_result_title_contains: top hit title {top_title!r} "
                f"does not contain {needle!r}"
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

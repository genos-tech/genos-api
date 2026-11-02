"""Routing eval: which tool does the model reach for first? (MIP-881)

One real model call per prompt, with the real system prompt and declarations,
reading only the first turn's function calls. No tool runs, so a case needs no
database, no OpenSearch and no fixture. Two things follow from that:

  * it covers what the behavior suite can't. No case in `cases.yaml` needs a
    calendar, to-do, PR or "me" tool, so that suite stays green whatever
    happens to them, and this is how a wrongly dropped tool becomes visible;
  * a surface can be measured before it exists. `declarations` is only data,
    so a candidate is a list of `ToolDeclaration`, and `translate` maps what
    the model called on it back to the registry tool the gold names.

It says nothing about whether the tool then did the right thing, or about
arguments. That is the behavior suite's job. The two are complementary.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from origin.search_engine.agent.languages import language_directive
from origin.search_engine.agent.prompts import agent_system_prompt
from origin.search_engine.llm import get_model_client
from origin.search_engine.llm.types import AgentMessage, CallUsage, FunctionCall, ToolDeclaration

log = logging.getLogger(__name__)

CASES_PATH = Path(__file__).with_name("routing_cases.yaml")
LANGUAGES = ("en", "ja")
# A transient 429 or 503 is the provider's, not the surface's.
_ATTEMPTS = 3
_BACKOFF_S = 2.0


@dataclass(frozen=True)
class RoutingCase:
    id: str
    lang: str
    query: str
    ok: frozenset[str] = frozenset()
    no_tool: bool = False


@dataclass
class Outcome:
    case_id: str
    lang: str
    arm: str
    tools: list[str] = field(default_factory=list)
    answered_in_text: bool = False
    passed: bool = False
    latency_ms: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


def load_cases(path: Path = CASES_PATH, languages: Iterable[str] = LANGUAGES) -> list[RoutingCase]:
    """Each YAML intent becomes one case per language."""
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cases = []
    for item in raw:
        for lang in languages:
            cases.append(
                RoutingCase(
                    id=item["id"],
                    lang=lang,
                    query=item[lang],
                    ok=frozenset(item.get("ok") or ()),
                    no_tool=bool(item.get("no_tool")),
                )
            )
    return cases


def system_instruction_for(lang: str, *, today_line: str, base: str | None = None) -> str:
    """The instruction a user of this language gets, in the controller's order."""
    text = f"{base if base is not None else agent_system_prompt()}\n\n{today_line}"
    directive = language_directive(lang)
    return f"{text}\n\n{directive}" if directive else text


def score(case: RoutingCase, tools: list[str]) -> bool:
    """Right first step. Parallel calls pass when any of them is a right one,
    since the model is not penalized for also gathering something else."""
    if case.no_tool:
        return not tools
    return bool(case.ok & set(tools))


def route_once(
    case: RoutingCase,
    *,
    arm: str,
    declarations: list[ToolDeclaration],
    system_instruction: str,
    model: str,
    client: Any = None,
    translate: Callable[[FunctionCall], str] | None = None,
) -> Outcome:
    """Ask once and score the first turn. A provider failure is recorded on the
    outcome, not raised: one bad call must not end a run of hundreds."""
    client = client or get_model_client()
    outcome = Outcome(case_id=case.id, lang=case.lang, arm=arm)
    messages = [AgentMessage(role="user", text=case.query)]
    for attempt in range(_ATTEMPTS):
        sink = CallUsage()
        calls: list[FunctionCall] = []
        text = False
        started = time.monotonic()
        try:
            for chunk, call in client.generate_step(
                messages=messages,
                tools=declarations,
                system_instruction=system_instruction,
                model_override=model,
                usage_sink=sink,
            ):
                text = text or bool(chunk)
                if call is not None:
                    calls.append(call)
        except Exception as exc:  # noqa: BLE001 — recorded, and retried below
            outcome.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("Routing case %s failed: %s", case.id, outcome.error)
            time.sleep(_BACKOFF_S * (attempt + 1))
            continue
        outcome.error = ""
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        outcome.tools = [translate(c) if translate else c.name for c in calls]
        outcome.answered_in_text = text and not calls
        outcome.prompt_tokens = sink.prompt_tokens
        outcome.cached_tokens = sink.cached_tokens
        outcome.output_tokens = sink.output_tokens
        outcome.passed = score(case, outcome.tools)
        break
    return outcome


def _percentile(values: list[int], fraction: float) -> int | None:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else None


def summarize(outcomes: list[Outcome]) -> dict[str, Any]:
    """One arm's numbers. An errored call says nothing about routing, so it is
    counted apart and left out of the rate."""
    scored = [o for o in outcomes if not o.error]
    by_lang: dict[str, list[Outcome]] = defaultdict(list)
    for o in scored:
        by_lang[o.lang].append(o)
    misses = Counter(
        (o.case_id, o.lang, ",".join(o.tools) or "(answered without a tool)")
        for o in scored
        if not o.passed
    )
    latencies = [o.latency_ms for o in scored]
    return {
        "calls": len(outcomes),
        "errors": len(outcomes) - len(scored),
        "pass_rate": round(sum(o.passed for o in scored) / max(len(scored), 1), 4),
        "pass_rate_by_lang": {
            lang: round(sum(o.passed for o in rows) / max(len(rows), 1), 4)
            for lang, rows in sorted(by_lang.items())
        },
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p90": _percentile(latencies, 0.9),
        "input_tokens_mean": round(
            statistics.mean(o.prompt_tokens + o.cached_tokens for o in scored)
        )
        if scored
        else 0,
        "cached_tokens_mean": round(statistics.mean(o.cached_tokens for o in scored))
        if scored
        else 0,
        "misses": [
            {"case": case, "lang": lang, "called": called, "count": count}
            for (case, lang, called), count in misses.most_common()
        ],
    }


def outcome_rows(outcomes: list[Outcome]) -> list[dict[str, Any]]:
    return [asdict(o) for o in outcomes]

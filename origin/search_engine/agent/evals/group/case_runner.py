"""The default `CaseRunner` of the multi-arm harness: one case, one arm, one result.

It reuses the existing runners; nothing here re-implements an agent loop.

  behaviour  `runner.run_behavior_case` (the real `run_agent` loop; the same function
             `agent_eval` and `agent_eval_compare` call). Its pass/fail is kept, and the
             group-only `expect:` keys (fact order, tool order, no leak into a tool result)
             are evaluated here on top of it.
  routing    `routing.route_once`: one model call, first-turn tool choice. The declarations
             and the system prompt are built PER CALL, inside the arm's overrides, because a
             flag that changes the tool surface would otherwise be measured against a
             surface built before the flag was applied (the baseline against itself).
  resume     `group.resume_runner.run_resume_case(case, overrides)`, owned by another
             agent. Imported lazily; when the module is not there the case is skipped, not
             failed.

Overrides are NOT applied here: the harness applies them around the call (and the resume
runner receives them too, as its contract says).
"""

from __future__ import annotations

import copy
import importlib
import json
import threading
from datetime import UTC, datetime
from typing import Any

from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.agent.evals.group.harness import (
    GROUP_EXPECT_KEYS,
    GROUP_ONLY_KEYS,
    ROUTING_MODEL,
    GroupCase,
    HarnessError,
    group_checks,
)
from origin.search_engine.agent.evals.group.usage import CaseResult

_seed_lock = threading.Lock()


def prepare_case(case: GroupCase) -> dict[str, Any]:
    """A private, runner-ready copy of the case: fixture resolved, group-only keys gone.

    `fixture: v1` becomes the runner's own `fixture: true` (it resolves the v1 team).
    `fixture: sequence` is resolved HERE, to its own team and the chosen viewer, and the
    `fixture` key is removed so the runner cannot resolve the v1 team over it. Cases with no
    fixture keep whatever team_id/user_id they pin.
    """
    raw = copy.deepcopy(case.raw)
    viewer = raw.get("viewer")
    for key in GROUP_ONLY_KEYS:
        raw.pop(key, None)
    if case.fixture == "v1":
        raw["fixture"] = True
    elif case.fixture == "sequence":
        info = resolve_sequence(viewer)
        raw["team_id"] = info["team_id"]
        raw["user_id"] = info["user_id"]
        _substitute(raw, {"{display_id}": info["display_id"], "{task_id}": str(info["task_id"])})
    return raw


def resolve_sequence(viewer: str | None) -> dict[str, Any]:
    """Ids of the sequence scenario for a viewer (`v1`, `v2`, `b`, `c`; default `v1`)."""
    from origin.search_engine.agent.evals.group import sequence  # noqa: PLC0415

    with _seed_lock:
        sequence.ensure_sequence()
    try:
        return sequence.viewer_info(viewer or "v1")
    except KeyError as exc:
        raise HarnessError(f"unknown sequence viewer {viewer!r}") from exc


def _substitute(raw: dict[str, Any], mapping: dict[str, str]) -> None:
    def sub(text: str) -> str:
        for old, new in mapping.items():
            text = text.replace(old, new)
        return text

    if isinstance(raw.get("query"), str):
        raw["query"] = sub(raw["query"])
    for turn in raw.get("turns") or []:
        if isinstance(turn.get("query"), str):
            turn["query"] = sub(turn["query"])


class DefaultCaseRunner:
    def __init__(self, *, judge: bool = False, routing_model: str = ROUTING_MODEL):
        self.judge = judge
        self.routing_model = routing_model

    def __call__(self, case: GroupCase, arm_name: str, overrides: dict[str, Any]) -> CaseResult:
        if case.kind == "resume":
            return self._resume(case, arm_name, overrides)
        if case.kind == "routing":
            return self._routing(case, arm_name)
        return self._behaviour(case, arm_name)

    # -- behaviour -------------------------------------------------------------- #

    def _behaviour(self, case: GroupCase, arm_name: str) -> CaseResult:
        from origin.search_engine.agent.evals import runner  # noqa: PLC0415

        raw = prepare_case(case)
        expect = raw.get("expect")
        if isinstance(expect, dict):
            own = {k: expect.pop(k) for k in list(expect) if k in GROUP_EXPECT_KEYS}
        else:
            own = {}
        with group_usage.capture_usage() as inner:
            native = runner.run_behavior_case(raw)
            judged = self._judge(native) if self.judge and native.answer else None
        tools = [str(t.get("tool_name")) for t in native.tool_results]
        result = group_usage.new_result(case.id, arm_name, case.lang)
        result["tools"] = tools
        result["answer"] = native.answer
        result["duration_ms"] = native.duration_ms
        reasons = list(native.failure_reasons)
        result["checks"] = (
            [{"name": "expect", "ok": False, "detail": r} for r in reasons]
            if reasons
            else [{"name": "expect", "ok": True, "detail": "every runner assertion held"}]
        )
        tool_text = json.dumps([t.get("result") for t in native.tool_results], default=str)
        extra = group_checks(
            own, answer=native.answer, tools=tools, tool_text=tool_text, calls=inner.calls
        )
        result["checks"] += extra
        result["passed"] = bool(native.passed) and all(c["ok"] for c in extra)
        if native.infra_error:
            result["error"] = "infra: LLM-provider failure after retry"
            result["passed"] = False
        if judged:
            result["judge"] = judged
        return result

    @staticmethod
    def _judge(native: Any) -> dict[str, Any] | None:
        from origin.search_engine.agent.evals.judge import judge_answer  # noqa: PLC0415
        from origin.search_engine.llm import spend  # noqa: PLC0415

        with spend.spend_context(surface="judge"):
            return judge_answer(
                query=native.query,
                sources=native.sources,
                answer=native.answer,
                tool_results=native.tool_results,
            )

    # -- routing ---------------------------------------------------------------- #

    def _routing(self, case: GroupCase, arm_name: str) -> CaseResult:
        from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: PLC0415
        from origin.search_engine.agent.evals import routing  # noqa: PLC0415
        from origin.search_engine.llm import get_model_client  # noqa: PLC0415

        raw = case.raw
        rcase = routing.RoutingCase(
            id=case.id,
            lang=case.lang,
            query=raw["query"],
            ok=frozenset(raw.get("ok") or ()),
            no_tool=bool(raw.get("no_tool")),
        )
        today = f"Today's date is {datetime.now(UTC):%Y-%m-%d (%A)} in the UTC timezone."
        outcome = routing.route_once(
            rcase,
            arm=arm_name,
            declarations=_build_tool_declarations(),
            system_instruction=routing.system_instruction_for(case.lang, today_line=today),
            model=self.routing_model,
            client=get_model_client(),
        )
        result = group_usage.new_result(case.id, arm_name, case.lang)
        result["tools"] = list(outcome.tools)
        result["passed"] = bool(outcome.passed) and not outcome.error
        result["duration_ms"] = outcome.latency_ms
        want = "no tool" if rcase.no_tool else f"one of {sorted(rcase.ok)}"
        result["checks"] = [
            {
                "name": "first_tool",
                "ok": result["passed"],
                "detail": f"want {want}, called {outcome.tools or '(answered without a tool)'}",
            }
        ]
        if outcome.error:
            result["error"] = outcome.error
        return result

    # -- resume ----------------------------------------------------------------- #

    def _resume(self, case: GroupCase, arm_name: str, overrides: dict[str, Any]) -> CaseResult:
        module = f"{__package__}.resume_runner"
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == module:
                result = group_usage.new_result(case.id, arm_name, case.lang)
                result["skipped"] = "resume_runner missing"
                return result
            raise
        returned = mod.run_resume_case(copy.deepcopy(case.raw), dict(overrides))
        result = group_usage.new_result(case.id, arm_name, case.lang)
        result.update(returned or {})  # type: ignore[typeddict-item]
        result["case_id"], result["arm"], result["lang"] = case.id, arm_name, case.lang
        return result

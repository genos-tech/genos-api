"""A stand-in for `group/usage.py` (agent H, branch `MIP-1271-eval-harness-multiarm`).

Used by `resume_runner` ONLY when that module is not on the stack, so the runner imports and
its tests run on their own. Same contract (AGENT_BRIEF "Eval-group contract"): `new_result`
and `capture_usage()` yielding an object with `request_id` and `as_dict()` of the usage fields.
It prices nothing: the numbers are the provider usage records it sees, summed, cost 0.0.
Delete this file when the stack carries H's `usage.py`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from origin.search_engine.llm import spend


def new_result(case_id: str, arm: str, lang: str = "en", **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case_id": case_id,
        "arm": arm,
        "lang": lang,
        "passed": False,
        "checks": [],
        "tools": [],
        "answer": "",
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "calls": 0,
        },
    }
    result.update(extra)
    return result


class _Capture:
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.input_tokens = self.output_tokens = self.cached_tokens = self.calls = 0

    def add(self, rec: Any) -> None:
        usage = getattr(rec, "usage", None)
        if usage is None or getattr(rec, "request_id", None) != self.request_id:
            return
        self.calls += 1
        self.input_tokens += int(usage.prompt_tokens) + int(usage.cached_tokens)
        self.cached_tokens += int(usage.cached_tokens)
        self.output_tokens += int(usage.output_tokens) + int(usage.thought_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_usd": 0.0,
            "calls": self.calls,
        }


@contextmanager
def capture_usage(*, surface: str = "eval") -> Iterator[_Capture]:
    previous = spend._recorder
    with spend.spend_context(surface=surface) as ctx:
        capture = _Capture(ctx.request_id)

        def tee(rec: Any) -> None:
            capture.add(rec)
            if previous is not None:
                previous(rec)

        spend.set_recorder(tee)
        try:
            yield capture
        finally:
            spend.set_recorder(previous)

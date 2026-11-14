"""Token and cost capture for one eval case, and the shared result shape.

`agent_eval_group` (the multi-arm harness) and every case runner that feeds it
(`kind: resume` in `resume_runner.py`, the item agents' own helpers) use this one
module, so a number in the matrix means the same thing whichever runner made it.

    with capture_usage() as usage:
        run_whatever_calls_the_model()
    usage.as_dict()
    # {"input_tokens": int, "output_tokens": int, "cached_tokens": int,
    #  "cost_usd": float, "calls": int}

HOW IT SEES A CALL. Every production LLM adapter (Gemini, Claude, OpenAI) ends a
call, in a `finally`, with `spend.record_llm_call(usage)` (llm/spend.py); that
reaches whatever recorder is installed. `capture_usage` chains a recorder in front
of the installed one, so:

  * the numbers are the provider's own usage metadata, the same source as the
    AI cost ledger (`AiSpendEvent`), with no estimate anywhere;
  * the real recorder still runs, so the ledger keeps filling when AI_COST_METER is
    on, and nothing here needs it on (the default is off) or needs a database;
  * a stub client that calls `spend.record_llm_call` (as the adapters do) is
    captured exactly like a real one, which is how the harness is tested.

COST is computed here from the catalog prices (`apis/llm_models.yaml`, through
`spend_recorder.price_llm_usage`), the same function that prices a ledger row: the
cached bucket at the cached rate, thinking at the output rate, tool-declaration
tokens as input, long-prompt tiers honoured. A model missing from the catalog is
priced 0 and counted in `unpriced_calls`; a total with `unpriced_calls > 0` is a
LOWER BOUND and the report says so.

ATTRIBUTION is by request id. `capture_usage` binds a spend context (re-entrant: a
context already bound, such as the runner's own `surface="eval"` one, is reused),
so every call made inside the block, on any thread that inherited the context,
lands in this capture and in no other. Records that reach the recorder with no
context (an uninstrumented path, or a worker thread that did not copy the context)
cannot be tied to a case. They are NOT dropped: they go to `UNATTRIBUTED`, and the
session cost cap (`SessionSpend`) counts them.

Field meaning (the contract in AGENT_BRIEF.md):
  input_tokens   ALL prompt tokens of every call: uncached + cached + cache-write +
                 tool-declaration tokens. `cached_tokens` is the part of it that was
                 served from the provider's cache.
  output_tokens  generated + thinking tokens (both are billed at the output rate).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, TypedDict

from origin.search_engine.llm import spend

# --------------------------------------------------------------------------- #
# The shared result shape                                                     #
# --------------------------------------------------------------------------- #


class UsageDict(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    calls: int


class CheckDict(TypedDict):
    name: str
    ok: bool
    detail: str


class CaseResult(TypedDict, total=False):
    """What one case run returns, from any runner (behaviour, routing, resume).

    Required keys are the AGENT_BRIEF contract; the optional ones are additions the
    harness fills in and every consumer must tolerate being absent:
      skipped      reason string when the case did not run (runner missing, arm needs
                   a flag that is not on origin yet, cost cap reached). A skipped case
                   is neither a pass nor a fail in the matrix.
      error        a runner crash, recorded instead of raised
      rep          repetition number (1-based) when --reps > 1
      duration_ms  wall time
      judge        judge scores when --judge was given
      unpriced_calls  calls whose model has no catalog price (cost is a lower bound)
    """

    case_id: str
    arm: str
    lang: str
    passed: bool
    checks: list[CheckDict]
    tools: list[str]
    answer: str
    usage: UsageDict
    skipped: str
    error: str
    rep: int
    duration_ms: int
    judge: dict[str, Any]
    unpriced_calls: int


def blank_usage() -> UsageDict:
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0, "calls": 0}


def new_result(case_id: str, arm: str, lang: str = "en", **extra: Any) -> CaseResult:
    """A result with every required key present (failed, nothing run yet)."""
    result: CaseResult = {
        "case_id": case_id,
        "arm": arm,
        "lang": lang,
        "passed": False,
        "checks": [],
        "tools": [],
        "answer": "",
        "usage": blank_usage(),
    }
    result.update(extra)  # type: ignore[typeddict-item]
    return result


# --------------------------------------------------------------------------- #
# Capture                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class UsageCapture:
    """Totals for the calls made inside one `capture_usage()` block."""

    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    thought_tokens: int = 0
    cost_usd: float = 0.0
    #: The same cost in milli-yen at the pinned rate card (the legacy `agent_eval` report's unit).
    cost_jpy_milli: int = 0
    calls: int = 0
    #: Calls on a model with no catalog price: cost_usd is a lower bound if > 0.
    unpriced_calls: int = 0
    #: Non-token spend (web search, rerank units, embeddings) priced per unit.
    other_cost_usd: float = 0.0
    models: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, rec: Any) -> None:
        """Fold one `SpendRecord` in. Never raises (accounting must not break a run)."""
        try:
            self._add(rec)
        except Exception:  # noqa: BLE001
            pass

    def _add(self, rec: Any) -> None:
        # Imported here: spend_recorder pulls in Django settings and credits.
        from origin.search_engine import spend_recorder  # noqa: PLC0415

        usage = rec.usage
        with self._lock:
            if usage is not None:
                usd_micro, jpy_milli, basis = spend_recorder.price_llm_usage(usage)
                if rec.unit_kind != spend.UNIT_TOKENS:
                    # Token-priced non-LLM spend (an embedding with a reported token
                    # count): money, but not a model call.
                    self.other_cost_usd += usd_micro / 1_000_000.0
                    self.cost_usd += usd_micro / 1_000_000.0
                    self.cost_jpy_milli += jpy_milli
                    return
                self.calls += 1
                prompt = max(int(usage.prompt_tokens), 0) + max(int(usage.tool_prompt_tokens), 0)
                cached = max(int(usage.cached_tokens), 0)
                write = max(int(usage.cache_write_tokens), 0)
                thought = max(int(usage.thought_tokens), 0)
                self.input_tokens += prompt + cached + write
                self.cached_tokens += cached
                self.cache_write_tokens += write
                self.output_tokens += max(int(usage.output_tokens), 0) + thought
                self.thought_tokens += thought
                self.cost_usd += usd_micro / 1_000_000.0
                self.cost_jpy_milli += jpy_milli
                if basis == "unpriced":
                    self.unpriced_calls += 1
                self.models[usage.model or "?"] = self.models.get(usage.model or "?", 0) + 1
            else:
                usd_micro, jpy_milli, basis = spend_recorder.price_units(
                    rec.unit_kind, rec.provider, rec.model, rec.units
                )
                self.other_cost_usd += usd_micro / 1_000_000.0
                self.cost_usd += usd_micro / 1_000_000.0
                self.cost_jpy_milli += jpy_milli
                if basis == "unpriced":
                    self.unpriced_calls += 1

    def as_dict(self) -> UsageDict:
        """The `usage` field of a `CaseResult`."""
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_tokens": self.cached_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "calls": self.calls,
            }


#: Records that reached the recorder while a capture was active but carried no
#: context of ours (see module docstring). Process-wide, cumulative.
UNATTRIBUTED = UsageCapture(request_id="unattributed")

_lock = threading.Lock()
_active: dict[str, list[UsageCapture]] = {}
_previous: dict[str, Any] = {"recorder": None, "installed": False}
_listeners: list[Any] = []


def _dispatch(rec: Any) -> None:
    """The recorder installed while any capture is open: tee to the captures, then on."""
    captures = _active.get(rec.request_id)
    if captures:
        for capture in list(captures):
            capture.add(rec)
    elif _active:
        # Open captures exist but this record belongs to none of them.
        UNATTRIBUTED.add(rec)
    for listener in list(_listeners):
        try:
            listener(rec, bool(captures))
        except Exception:  # noqa: BLE001
            pass
    previous = _previous["recorder"]
    if previous is not None:
        try:
            previous(rec)
        except Exception:  # noqa: BLE001 — same contract as spend._emit
            pass


def _install() -> None:
    if not _previous["installed"]:
        _previous["recorder"] = spend._recorder
        spend.set_recorder(_dispatch)
        _previous["installed"] = True


def _uninstall() -> None:
    if _previous["installed"] and not _active:
        spend.set_recorder(_previous["recorder"])
        _previous["recorder"] = None
        _previous["installed"] = False


@contextmanager
def capture_usage(*, surface: str = "eval") -> Iterator[UsageCapture]:
    """Capture tokens and cost for every model call made inside the block.

    Re-entrant and safe under threads: each block has its own `UsageCapture`, found
    by the request id of the spend context it binds (or reuses). Two blocks that end
    up on the SAME request id (nested) both receive the calls.
    """
    with spend.spend_context(surface=surface) as ctx:
        capture = UsageCapture(request_id=ctx.request_id)
        with _lock:
            _install()
            _active.setdefault(ctx.request_id, []).append(capture)
        try:
            yield capture
        finally:
            with _lock:
                bucket = _active.get(ctx.request_id, [])
                if capture in bucket:
                    bucket.remove(capture)
                if not bucket:
                    _active.pop(ctx.request_id, None)
                _uninstall()


@contextmanager
def listen(callback: Any) -> Iterator[None]:
    """Call `callback(record, attributed)` for every record while open (used by the cost
    cap to see spend that no case owns). Installs the recorder like a capture does."""
    with _lock:
        _install()
        _listeners.append(callback)
    try:
        yield
    finally:
        with _lock:
            if callback in _listeners:
                _listeners.remove(callback)
            _uninstall()


# --------------------------------------------------------------------------- #
# Catalog prices, for projections that make no call                           #
# --------------------------------------------------------------------------- #


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float | None:
    """USD for a hypothetical call from catalog prices; None when the model has none.

    `input_tokens` is the TOTAL prompt (cached included), as in `UsageCapture`.
    """
    from django.conf import settings  # noqa: PLC0415

    catalog = getattr(settings, "LLM_CATALOG", None)
    price = catalog.price_for(model) if catalog is not None else None
    if price is None:
        return None
    cached = max(min(int(cached_tokens), int(input_tokens)), 0)
    fresh = max(int(input_tokens) - cached, 0)
    price = price.for_prompt(int(input_tokens))
    return (
        fresh * price.input
        + cached * price.cached_input
        + max(int(output_tokens), 0) * price.output
    ) / 1_000_000.0


def ledger_total_usd(since: Any) -> float:
    """Sum of `AiSpendEvent.cost_usd_micro` written at or after `since`, in USD.

    0.0 when the meter is off or the table is unreachable. This is the SECOND opinion
    the cost cap reads next to the in-process captures: it also sees spend made by
    code that does not run under a capture (judge calls, retries inside the runner).
    """
    try:
        from django.conf import settings  # noqa: PLC0415
        from django.db.models import Sum  # noqa: PLC0415

        if not settings.SEARCH_ENGINE.get("AI_COST_METER", False):
            return 0.0
        from origin.search_engine.models import AiSpendEvent  # noqa: PLC0415

        total = AiSpendEvent.objects.filter(created_at__gte=since).aggregate(
            t=Sum("cost_usd_micro")
        )["t"]
        return int(total or 0) / 1_000_000.0
    except Exception:  # noqa: BLE001
        return 0.0

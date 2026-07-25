"""Provider-neutral spend accounting seam.

This module is the boundary between "a paid call happened" and "a row
gets written". It holds a `ContextVar` describing the logical request in
flight, and an INJECTABLE recorder that the Django layer registers at
app-ready (`search_engine/apps.py`).

**No Django imports here, deliberately.** The whole premise of the `llm`
package is that no SDK type and no ORM type leaks across it (see
`llm/types.py`). The adapters must be importable, and testable, without
a database — so they call `record_llm_call()` and never see a model.
`_recorder` defaults to a no-op, which is also what makes the flag-off
path free.

Three design decisions worth knowing before changing anything:

**1. Recording happens at the ADAPTER, not the call site.** There are
ten `generate_step` callers and exactly one of them used to pass a
usage sink; the other nine — query rewrite, rerank, three summary
paths, judge, eval — spent real money invisibly. Threading a kwarg
through all of them would have fixed today's nine and none of
tomorrow's. Recording where the provider response is parsed makes the
"forgot to instrument the new call site" bug unexpressible.

**2. It happens in a `finally`.** `generate_step` implementations are
GENERATORS. A tail call is skipped by a mid-stream raise and by
`GeneratorExit`, which is exactly the case that matters: a call that
streamed thousands of output tokens and then died is fully billed by
the provider. Recording at the tail would have relocated that blind
spot rather than closed it.

**3. Unattributed spend is RECORDED, not dropped.** A call with no
bound context still writes a row under `surface="unattributed"`.
Invoice reconciliation is the only end-to-end check that the meter is
correct, and a silently-dropped path makes it reconcile to "close
enough" forever. An unattributed line in the cost report is the
tripwire for the next uninstrumented entry point.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Iterator

from origin.search_engine.llm.types import CallUsage

log = logging.getLogger(__name__)

# Surface for spend with no bound context — see decision 3 above.
SURFACE_UNATTRIBUTED = "unattributed"

# Non-token unit kinds, for spend that isn't measured in tokens.
UNIT_TOKENS = ""
UNIT_SEARCH = "search"
UNIT_EMBED = "embed"
UNIT_RERANK = "rerank"


@dataclass
class SpendContext:
    """The logical request every paid call in flight belongs to.

    `request_id` is the primary attribution key and is minted by the
    entry point BEFORE anything happens. `run_id` is a nullable
    reference bound later, if at all: `AgentRun` is created part-way
    into the ask view (an LLM call already fired by then), and half the
    paid surfaces — plain search, both summary endpoints, crons, evals —
    never create one. Keying attribution on the run is precisely the
    mistake that makes the existing `AgentLlmCall` telemetry record
    nothing for the entire eval suite.

    `plan` and `effort` are resolved ONCE here and denormalized onto
    every row. Resolving per row would put a tier lookup — a cache miss
    away from a DB query — on the request path once per LLM call.
    """

    request_id: str
    surface: str
    user_id: str = ""
    team_id: str = ""
    plan: str = ""
    effort: str = ""
    run_id: str | None = None
    # Running total for this request. Read by the per-request ceiling;
    # the authoritative number is still the sum of the rows.
    cost_jpy_milli: int = 0
    events: int = 0
    # Set when a ceiling stopped this request, for the rollup's result.
    ceiling_hit: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpendRecord:
    """One billable call, provider-neutral, as handed to the recorder."""

    request_id: str
    surface: str
    purpose: str
    user_id: str
    team_id: str
    plan: str
    effort: str
    run_id: str | None
    provider: str
    model: str
    usage: CallUsage | None
    unit_kind: str
    units: int
    latency_ms: int
    error: str
    attempt_no: int
    # The provider's own billable quantity when it is neither tokens nor
    # `units` (Vertex embeddings bill characters). Defaulted so every
    # existing construction stays valid.
    billable_units: int = 0


_context: ContextVar[SpendContext | None] = ContextVar("ai_spend_context", default=None)
_purpose: ContextVar[str] = ContextVar("ai_spend_purpose", default="")

# Registered by `SearchEngineConfig.ready()`. None = no-op, which is
# what keeps the flag-off path and every import-only test free.
_recorder: Callable[[SpendRecord], None] | None = None

# Purposes already warned about, so an uninstrumented loop can't flood
# the log. The ROW is the signal; the log line is a hint.
_warned_unattributed: set[str] = set()


def set_recorder(fn: Callable[[SpendRecord], None] | None) -> None:
    """Install the Django-aware recorder (or None to uninstall)."""
    global _recorder
    _recorder = fn


def current_context() -> SpendContext | None:
    return _context.get()


def current_purpose() -> str:
    return _purpose.get()


@contextmanager
def spend_context(
    *,
    surface: str,
    user_id: str = "",
    team_id: str = "",
    plan: str = "",
    effort: str = "",
    run_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[SpendContext]:
    """Bind a logical request for the duration of the block.

    RE-ENTRANT: if a context is already bound, the existing one is
    yielded unchanged and nothing is rebound. `search()` is called both
    by the search endpoint and by the `search_kb` tool inside an ask, so
    the inner bind must not fragment the ask's spend across two
    request_ids.

    Always paired with a reset in `finally` — gunicorn runs threaded and
    reuses worker threads across requests, so a leaked context would
    bill the next user's tokens to this one. That is worse than a
    missing row: silently wrong rather than visibly incomplete. There is
    a test asserting the var is None after every entry point returns.
    """
    existing = _context.get()
    if existing is not None:
        yield existing
        return

    ctx = SpendContext(
        request_id=request_id or str(uuid.uuid4()),
        surface=surface,
        user_id=str(user_id or ""),
        team_id=str(team_id or ""),
        plan=plan or "",
        effort=effort or "",
        run_id=str(run_id) if run_id else None,
    )
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


@contextmanager
def spend_purpose(name: str) -> Iterator[None]:
    """Tag the calls made inside this block (`rewrite`, `rerank`, ...).

    Token-reset rather than set-and-forget: the reranker's purpose must
    not leak into the next loop step's row.
    """
    token = _purpose.set(name or "")
    try:
        yield
    finally:
        _purpose.reset(token)


def bind_run_id(run_id: str | None) -> None:
    """Attach a run id to the active context once it exists.

    Late-bound on purpose — see `SpendContext.run_id`.
    """
    ctx = _context.get()
    if ctx is not None and run_id:
        ctx.run_id = str(run_id)


def request_cost_jpy_milli() -> int:
    """Spend accumulated by the active request so far, in milli-yen.

    In-memory and free to read — the recorder adds to it as each row is
    written. The authoritative number is still the sum of the rows; this
    exists so a per-request ceiling can be checked between agent loop
    steps without a query on the request path.
    """
    ctx = _context.get()
    return ctx.cost_jpy_milli if ctx is not None else 0


def mark_ceiling_hit() -> None:
    """Flag that a ceiling stopped this request, for the rollup."""
    ctx = _context.get()
    if ctx is not None:
        ctx.ceiling_hit = True


def copy_context_for_thread():
    """`contextvars.copy_context()` for handing to a worker thread.

    Bare threads and `ThreadPoolExecutor` do NOT inherit context vars,
    so a parallel tool batch would otherwise run its rewrite/rerank
    calls with no context at all and land in `unattributed`.
    """
    import contextvars  # noqa: PLC0415

    return contextvars.copy_context()


def _emit(record: SpendRecord) -> None:
    """Hand a record to the recorder. Never raises."""
    if _recorder is None:
        return
    try:
        _recorder(record)
    except Exception:  # noqa: BLE001 — accounting must never break generation
        log.debug("spend recorder failed", exc_info=True)


def _resolve(purpose: str) -> tuple[SpendContext, bool]:
    """The active context, or a synthetic unattributed one."""
    ctx = _context.get()
    if ctx is not None:
        return ctx, False
    key = purpose or "(none)"
    if key not in _warned_unattributed:
        _warned_unattributed.add(key)
        # WARNING, not ERROR: CronCommand's tripwire watches ERROR on
        # the `origin` logger and would red an otherwise-fine job.
        log.warning(
            "AI spend recorded with no request context (purpose=%s). It is "
            "attributed to '%s' and will show as its own line in the cost "
            "report — an entry point is missing a spend_context() bind.",
            key,
            SURFACE_UNATTRIBUTED,
        )
    return SpendContext(request_id=str(uuid.uuid4()), surface=SURFACE_UNATTRIBUTED), True


def record_llm_call(
    usage: CallUsage,
    *,
    latency_ms: int = 0,
    error: str = "",
    attempt_no: int = 1,
) -> None:
    """Record one LLM call. Called from the adapters' `finally`.

    `usage` may be all-zero when the call died before the provider
    reported anything — the row is still written, because "a call
    happened and failed" is a fact the reconciliation needs.
    """
    purpose = _purpose.get()
    ctx, _ = _resolve(purpose)
    ctx.events += 1
    _emit(
        SpendRecord(
            request_id=ctx.request_id,
            surface=ctx.surface,
            purpose=purpose,
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            plan=ctx.plan,
            effort=ctx.effort,
            run_id=ctx.run_id,
            provider=usage.provider,
            model=usage.model,
            usage=usage,
            unit_kind=UNIT_TOKENS,
            units=0,
            latency_ms=latency_ms,
            error=error,
            attempt_no=attempt_no,
        )
    )


def record_units(
    *,
    unit_kind: str,
    units: int,
    provider: str,
    model: str = "",
    latency_ms: int = 0,
    error: str = "",
    tokens: int = 0,
    billable_units: int = 0,
) -> None:
    """Record non-token spend — a web search, an embedding, a rerank.

    `units` is the natural count for the call: documents reranked, texts
    embedded, searches issued. It is what the call DID, and it is never
    what the call COST.

    `tokens`, when the provider reports one, is what it cost. Passing it
    builds a `CallUsage`, which is all the recorder needs to price the
    row from the catalog exactly like an LLM call — no second pricing
    path. Embedding APIs do return this (OpenAI `usage.prompt_tokens`,
    Vertex `statistics.token_count`); the ledger used to discard it and
    file every embedding as `unpriced`, which put a real billing line
    permanently outside the totals.

    `billable_units` is the provider's OWN stated billable quantity when
    it differs from tokens — Vertex embeddings report
    `billable_character_count`. Stored unpriced, purely so that a change
    of billing unit stays reconcilable from data already captured
    instead of needing the calls to be made again.
    """
    purpose = _purpose.get()
    ctx, _ = _resolve(purpose)
    ctx.events += 1
    usage = None
    if tokens > 0:
        usage = CallUsage()
        usage.provider = provider
        usage.model = model
        usage.prompt_tokens = int(tokens)
    _emit(
        SpendRecord(
            request_id=ctx.request_id,
            surface=ctx.surface,
            purpose=purpose,
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            plan=ctx.plan,
            effort=ctx.effort,
            run_id=ctx.run_id,
            provider=provider,
            model=model,
            usage=usage,
            unit_kind=unit_kind,
            units=int(units),
            billable_units=int(billable_units),
            latency_ms=latency_ms,
            error=error,
            attempt_no=1,
        )
    )

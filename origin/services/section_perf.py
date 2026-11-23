"""Per-section query counts and timings for `get_my_briefing` (MIP-2179).

Production `get_my_briefing` makes ~350 queries per call; a local board seeded to the same row
counts makes ~150. To find which section holds the excess, `build_briefing` runs each section
inside `with section("name"):` and, when `AGENT_CALL_LOG` is on (it is on in production), this
module counts the queries and the milliseconds spent in each. The result goes on the request's
`perf.request` line, beside `db_n=` and `tool=`:

    perf.request ... tool=get_my_briefing result_chars=9000 sections=setup:4/3,changed_for_me:60/40,...

Each entry is `<section>:<queries>/<ms>`. Section names are fixed words in this file. No content,
no id, no text, no argument ever goes in. The queries outside any section (auth, tier gate,
member_events, notice) are `db_n` minus the sum of the entries.

With the switch off nothing is installed and nothing is recorded: `collecting()` yields at once
and `section()` is a no-op. The tool's result is never touched, and nothing here may fail a call.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator

from django.db import connections

from origin.services import agent_calls

log = logging.getLogger(__name__)

_local = threading.local()


class _Collector:
    """A `connection.execute_wrapper` that charges each query to the section that is open."""

    def __init__(self) -> None:
        self.current: str | None = None
        self.totals: dict[str, list[float]] = {}  # name -> [queries, seconds]

    def __call__(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            if self.current is not None:
                entry = self.totals.setdefault(self.current, [0, 0.0])
                entry[0] += 1
                entry[1] += time.perf_counter() - started


@contextlib.contextmanager
def collecting() -> Iterator[None]:
    """Count per-section queries for the body, then leave the summary for the perf line."""
    if not agent_calls.enabled():
        yield
        return
    collector = _Collector()
    stack = contextlib.ExitStack()
    try:
        for connection in connections.all():
            stack.enter_context(connection.execute_wrapper(collector))
        _local.collector = collector
    except Exception:  # noqa: BLE001 - measuring must never fail a call
        log.debug("section perf: wrapper not installed", exc_info=True)
    try:
        yield
    finally:
        try:
            _local.summary = _format(collector)
            _local.collector = None
        except Exception:  # noqa: BLE001
            pass
        stack.close()


@contextlib.contextmanager
def section(name: str) -> Iterator[None]:
    """Charge the queries run in the body to `name` (repeated names add up)."""
    collector = getattr(_local, "collector", None)
    if collector is None:
        yield
        return
    previous = collector.current
    collector.current = name
    try:
        yield
    finally:
        collector.current = previous


def _format(collector: _Collector) -> str:
    return ",".join(
        f"{name}:{int(entry[0])}/{round(entry[1] * 1000)}"
        for name, entry in collector.totals.items()
    )


def reset() -> None:
    """Forget a summary left by an earlier request on this thread."""
    _local.summary = ""
    _local.collector = None


def take() -> str:
    """` sections=...` for the perf line, once; empty when nothing was collected."""
    summary = getattr(_local, "summary", "")
    _local.summary = ""
    return f" sections={summary}" if summary else ""

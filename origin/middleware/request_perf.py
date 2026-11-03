"""One log line per API request, and one a minute for the container's CPU (MIP-956).

Production runs ONE gunicorn worker (gthread, 8 threads) on a fraction of
a vCPU, and Railway's HTTP log has only the total time of a request. A slow
answer can be waiting for the CPU, waiting for the database, or issuing too
many queries; the totals cannot say which. This middleware logs, on the
`origin.perf` logger at INFO (the `origin` logger's console handler is what
reaches Railway), the numbers that can:

    perf.request method=GET route=api/v2/task/getTask/ status=200 wall_ms=41 cpu_ms=12 db_n=6 db_ms=9 inflight=3 bytes=1234

  * `wall_ms`   the whole stack below this middleware, wall clock.
  * `cpu_ms`    CPU this THREAD used meanwhile (`time.thread_time()`).
                `wall_ms` far above `cpu_ms + db_ms` is time spent waiting
                for the GIL or for the CPU quota.
  * `db_n`, `db_ms`  queries and time in them, on every connection, from
                this thread (a view that hands work to another thread does
                not count what that thread runs).
  * `inflight`  requests this process was handling when this one STARTED,
                itself included, whether or not they are logged (a skipped
                `/media/` read holds a thread too). Its thread pool is 8 wide.
  * `bytes`     the Content-Length header; `-` when there is none. A
                streaming body is never read (and its time is not counted:
                the line is written when the headers are ready).
  * `route`     the URL TEMPLATE (`api/v2/milestone/<int:milestone_id>/`),
                `unresolved` when nothing matched. Never the path, the
                query string, a header, a body or a user.

Every minute or so, from whichever request notices, it also logs how much
the container was CPU-throttled since the last sample, read from the
cgroup v2 file `REQUEST_PERF_CPU_STAT_PATH`:

    perf.cpu window_s=60 periods=123 throttled=17 throttled_ms=1520 usage_ms=2300

Railway's CPU graph averages over minutes and cannot show a quota that bites
in bursts; `throttled` out of `periods` can. There is no background thread,
so a window is as long as the gap between two requests when traffic is
thin. A file that is missing, unreadable or not cgroup v2 turns the sampler
off for good, quietly (a DEBUG line, nothing at INFO).

`REQUEST_PERF_LOG=0|false|no|off` removes the middleware (it is on by
default). Nothing here may break a request: every measurement and every log
call is guarded, and the response is always returned.

Skipped: OPTIONS, the health probes, `/media/`, `/static/`.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.db import connections

log = logging.getLogger("origin.perf")

DEFAULT_CPU_STAT_PATH = "/sys/fs/cgroup/cpu.stat"
CPU_SAMPLE_INTERVAL_S = 60.0

# `railway.toml`'s healthcheckPath, and the alerting probe beside it.
HEALTH_PREFIX = "/api/v2/health/"

_WHITESPACE = re.compile(r"\s+")


class InflightCounter:
    """Requests being handled in this process. A lock, not a bare `+= 1`:
    the worker has several threads and the GIL does not make that atomic."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def enter(self) -> int:
        """Count one more request; returns the count, itself included."""
        with self._lock:
            self._n += 1
            return self._n

    def leave(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def value(self) -> int:
        with self._lock:
            return self._n


# One per process, whatever number of middleware instances Django builds.
inflight = InflightCounter()


class QueryStats:
    """A `connection.execute_wrapper`: counts the queries a thread runs and
    the time they took. One per request, used from one thread, so no lock."""

    __slots__ = ("count", "seconds")

    def __init__(self) -> None:
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.perf_counter() - started


class _Span:
    """What one logged request measures. `end` never raises."""

    def __init__(self, inflight_at_start: int) -> None:
        self.stats = QueryStats()
        self.inflight = inflight_at_start
        self.wall_ms = 0
        self.cpu_ms = 0
        self._open = True
        self._stack = contextlib.ExitStack()
        try:
            for connection in connections.all():
                self._stack.enter_context(connection.execute_wrapper(self.stats))
        except Exception:  # noqa: BLE001 — count what we can
            log.debug("perf: query wrapper not installed", exc_info=True)
        # Clocks last, so the set-up above is not charged to the request.
        self._wall0 = time.perf_counter()
        self._cpu0 = time.thread_time()

    def end(self) -> None:
        """Stop the clocks first, then undo the set-up. Idempotent."""
        if not self._open:
            return
        self._open = False
        try:
            self.wall_ms = round((time.perf_counter() - self._wall0) * 1000)
            self.cpu_ms = round((time.thread_time() - self._cpu0) * 1000)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stack.close()
        except Exception:  # noqa: BLE001
            log.debug("perf: query wrapper not removed", exc_info=True)


class CpuThrottleSampler:
    """Logs the container's CPU throttling since the previous sample, at
    most once per `interval_s`. The first sample only records a baseline.

    `tick()` is called per request and is a clock read until a sample is
    due. Only one thread samples at a time; the others move on."""

    KEYS = ("nr_periods", "nr_throttled", "throttled_usec", "usage_usec")

    def __init__(self, path, interval_s=CPU_SAMPLE_INTERVAL_S, clock=time.monotonic) -> None:
        self.path = path
        self.interval_s = interval_s
        self.clock = clock
        self.disabled = not path
        self._lock = threading.Lock()
        self._next_at: float | None = None
        self._prev: tuple[float, dict[str, int]] | None = None

    def read(self) -> dict[str, int]:
        """The four counters; raises on a missing file or an odd one."""
        with open(self.path, encoding="ascii") as handle:
            text = handle.read(65536)
        values: dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in self.KEYS:
                values[parts[0]] = int(parts[1])
        missing = [key for key in self.KEYS if key not in values]
        if missing:
            raise ValueError(f"cpu.stat has no {missing}")
        return values

    def tick(self) -> None:
        if self.disabled:
            return
        now = self.clock()
        if self._next_at is not None and now < self._next_at:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._next_at is not None and now < self._next_at:
                return
            try:
                current = self.read()
            except Exception as exc:  # noqa: BLE001 — off for good, quietly
                self.disabled = True
                log.debug("perf.cpu sampler off: %s", type(exc).__name__)
                return
            self._next_at = now + self.interval_s
            previous, self._prev = self._prev, (now, current)
            if previous is None:
                return
            then, before = previous
            delta = {key: current[key] - before[key] for key in self.KEYS}
            if any(value < 0 for value in delta.values()):
                return  # the counters restarted; this window means nothing
            log.info(
                "perf.cpu window_s=%d periods=%d throttled=%d throttled_ms=%d usage_ms=%d",
                round(now - then),
                delta["nr_periods"],
                delta["nr_throttled"],
                round(delta["throttled_usec"] / 1000),
                round(delta["usage_usec"] / 1000),
            )
        finally:
            self._lock.release()


def _skip_prefixes() -> tuple[str, ...]:
    def rooted(url: str) -> str:
        return "/" + url.lstrip("/")

    prefixes = [HEALTH_PREFIX]
    for name in ("MEDIA_URL", "STATIC_URL"):
        url = getattr(settings, name, None)
        if url:
            prefixes.append(rooted(str(url)))
    return tuple(prefixes)


def _route(request) -> str:
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    if not route:
        return "unresolved"
    return _WHITESPACE.sub("", str(route)) or "unresolved"


def _method(request) -> str:
    method = str(getattr(request, "method", "") or "")
    return method if method.isalpha() and len(method) <= 10 else "-"


def _bytes(response) -> str:
    length = str(response.get("Content-Length", "") or "")
    return length if length.isdigit() else "-"


class RequestPerfMiddleware:
    def __init__(self, get_response):
        if not getattr(settings, "REQUEST_PERF_LOG", True):
            raise MiddlewareNotUsed("REQUEST_PERF_LOG is off")
        self.get_response = get_response
        self._skip = _skip_prefixes()
        self._cpu = CpuThrottleSampler(
            getattr(settings, "REQUEST_PERF_CPU_STAT_PATH", DEFAULT_CPU_STAT_PATH)
        )

    def _skipped(self, request) -> bool:
        try:
            return request.method == "OPTIONS" or request.path_info.startswith(self._skip)
        except Exception:  # noqa: BLE001
            return True

    def _begin(self, inflight_at_start: int) -> _Span | None:
        try:
            return _Span(inflight_at_start)
        except Exception:  # noqa: BLE001
            return None

    def __call__(self, request):
        if not log.isEnabledFor(logging.INFO):
            return self.get_response(request)
        # Every request counts toward `inflight`, logged or not: a burst of
        # `/media/` reads holds the same eight threads as an API call.
        count = inflight.enter()
        try:
            return self._handle(request, count)
        finally:
            inflight.leave()

    def _handle(self, request, count: int):
        if self._skipped(request):
            return self.get_response(request)
        span = self._begin(count)
        if span is None:
            return self.get_response(request)
        try:
            response = self.get_response(request)
        finally:
            span.end()
        try:
            log.info(
                "perf.request method=%s route=%s status=%d wall_ms=%d cpu_ms=%d "
                "db_n=%d db_ms=%d inflight=%d bytes=%s",
                _method(request),
                _route(request),
                response.status_code,
                span.wall_ms,
                span.cpu_ms,
                span.stats.count,
                round(span.stats.seconds * 1000),
                span.inflight,
                _bytes(response),
            )
        except Exception:  # noqa: BLE001 — a log line must never fail a request
            pass
        try:
            self._cpu.tick()
        except Exception:  # noqa: BLE001
            pass
        return response

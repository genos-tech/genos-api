"""The per-request `perf.request` line and the `perf.cpu` throttling line (MIP-956).

Pinned, roughly in order of how silently each would break:

1. **The line's shape.** Someone parses it: keys, order and units are a
   contract, and the route must be the URL template, never a path value,
   a query string or anything about the caller.
2. **The numbers mean what they say:** queries counted on the connection,
   DB time inside `db_ms`, thread CPU apart from wall time, `inflight`
   rising for overlapping requests.
3. **What is skipped:** OPTIONS, the health probes, `/media/`, `/static/`.
4. **It never breaks a request,** and never leaves a wrapper or a count
   behind when one fails.
5. **The CPU sampler** logs deltas from a cgroup v2 file, records a
   baseline first, and turns itself off quietly on a file it can't use.
6. **The switch** removes the middleware.
"""

from __future__ import annotations

import re
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import MiddlewareNotUsed
from django.db import connection, connections
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

from origin.middleware import request_perf
from origin.middleware.request_perf import (
    CpuThrottleSampler,
    InflightCounter,
    RequestPerfMiddleware,
    inflight,
)

LOGGER = "origin.perf"
MIDDLEWARE_PATH = "origin.middleware.request_perf.RequestPerfMiddleware"

# Exactly the keys, in exactly this order.
REQUEST_LINE = re.compile(
    r"^perf\.request method=(?P<method>[A-Z]+|-) route=(?P<route>\S+) "
    r"status=(?P<status>\d+) wall_ms=(?P<wall_ms>\d+) cpu_ms=(?P<cpu_ms>\d+) "
    r"db_n=(?P<db_n>\d+) db_ms=(?P<db_ms>\d+) inflight=(?P<inflight>\d+) "
    r"bytes=(?P<bytes>\d+|-)$"
)
CPU_LINE = re.compile(
    r"^perf\.cpu window_s=(?P<window_s>\d+) periods=(?P<periods>\d+) "
    r"throttled=(?P<throttled>\d+) throttled_ms=(?P<throttled_ms>\d+) "
    r"usage_ms=(?P<usage_ms>\d+)$"
)

CPU_STAT = (
    "usage_usec {usage}\nuser_usec 0\nsystem_usec 0\n"
    "nr_periods {periods}\nnr_throttled {throttled}\nthrottled_usec {throttled_usec}\n"
    "nr_bursts 0\nburst_usec 0\n"
)


def _messages(cm, prefix: str) -> list[str]:
    return [m for m in (r.getMessage() for r in cm.records) if m.startswith(prefix)]


def _request_lines(cm) -> list[dict[str, str]]:
    lines = _messages(cm, "perf.request")
    parsed = []
    for line in lines:
        match = REQUEST_LINE.match(line)
        assert match, f"not in the contract shape: {line!r}"
        parsed.append(match.groupdict())
    return parsed


def _view(path: str, work=None, response=None):
    """A `get_response` that resolves `path` the way the URL resolver would
    (the middleware reads `request.resolver_match` after the response), does
    `work(request)` and answers."""

    def get_response(request):
        request.resolver_match = resolve(request.path_info)
        if work:
            work(request)
        return response if response is not None else HttpResponse("ok")

    return get_response


def _count_users(times: int):
    User = get_user_model()

    def work(request):
        for _ in range(times):
            User.objects.count()

    return work


class RequestLineTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The first resolve imports every URLconf (~1 s of CPU), which would
        # otherwise land in the first test's `cpu_ms`.
        resolve("/api/v2/milestone/7/")

    def setUp(self):
        self.factory = RequestFactory()

    def _call(self, path="/api/v2/milestone/7/", get_response=None, method="GET", **extra):
        middleware = RequestPerfMiddleware(get_response or _view(path))
        return middleware(getattr(self.factory, method.lower())(path, **extra))

    # ── 1. the shape ────────────────────────────────────────────────

    def test_one_line_in_the_contract_shape(self):
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self._call(get_response=_view("/api/v2/milestone/7/", _count_users(3)))
        self.assertEqual(response.status_code, 200)
        lines = _request_lines(cm)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["method"], "GET")
        self.assertEqual(lines[0]["status"], "200")
        self.assertEqual(cm.records[0].levelname, "INFO")
        self.assertEqual(cm.records[0].name, LOGGER)

    def test_the_route_is_the_template_without_ids_or_query_string(self):
        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call(
                "/api/v2/milestone/123456/assignees/?token=hunter2&email=a@b.co",
                get_response=_view("/api/v2/milestone/123456/assignees/"),
            )
        (line,) = _messages(cm, "perf.request")
        self.assertIn("route=api/v2/milestone/<int:milestone_id>/assignees/ ", line)
        for leaked in ("123456", "hunter2", "token", "email", "a@b.co", "?"):
            self.assertNotIn(leaked, line)

    def test_an_unresolved_route_is_named_so(self):
        def get_response(request):
            return HttpResponse(status=404)

        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call("/api/v2/no/such/thing/9/", get_response=get_response)
        (line,) = _request_lines(cm)
        self.assertEqual(line["route"], "unresolved")
        self.assertEqual(line["status"], "404")

    def test_the_line_names_nothing_about_the_caller(self):
        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call(
                "/api/v2/milestone/5/",
                get_response=_view("/api/v2/milestone/5/"),
                HTTP_AUTHORIZATION="Bearer eyJhbGciOi.eyJ1c2VyX2lk.sig",
                HTTP_COOKIE="refresh=abc",
                HTTP_X_FORWARDED_FOR="203.0.113.9",
            )
        # The whole line matches the whitelisted shape; nothing else fits.
        self.assertEqual(len(_request_lines(cm)), 1)
        text = cm.output[0]
        for leaked in ("Bearer", "eyJ", "refresh", "203.0.113.9"):
            self.assertNotIn(leaked, text)

    def test_bytes_is_the_content_length_or_a_dash(self):
        sized = HttpResponse("hello")
        sized["Content-Length"] = "1234"
        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call(get_response=_view("/api/v2/milestone/7/", response=sized))
            self._call(get_response=_view("/api/v2/milestone/7/"))
        first, second = _request_lines(cm)
        self.assertEqual(first["bytes"], "1234")
        self.assertEqual(second["bytes"], "-")

    def test_a_streaming_body_is_not_consumed(self):
        consumed = []

        def body():
            consumed.append(True)
            yield b"chunk"

        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self._call(
                get_response=_view("/api/v2/milestone/7/", response=StreamingHttpResponse(body()))
            )
        self.assertEqual(consumed, [])
        self.assertEqual(_request_lines(cm)[0]["bytes"], "-")
        self.assertEqual(b"".join(response.streaming_content), b"chunk")

    def test_a_hostile_method_name_is_not_echoed(self):
        request = self.factory.get("/api/v2/milestone/7/")
        request.method = "GET status=500 route=x"
        with self.assertLogs(LOGGER, "INFO") as cm:
            RequestPerfMiddleware(_view("/api/v2/milestone/7/"))(request)
        self.assertEqual(_request_lines(cm)[0]["method"], "-")
        self.assertNotIn("status=500", cm.output[0])

    # ── 2. the numbers ──────────────────────────────────────────────

    def test_counts_the_queries_a_view_issues(self):
        for times in (0, 1, 5):
            with self.subTest(queries=times), self.assertLogs(LOGGER, "INFO") as cm:
                self._call(get_response=_view("/api/v2/milestone/7/", _count_users(times)))
            self.assertEqual(_request_lines(cm)[0]["db_n"], str(times))

    def test_db_ms_is_the_time_inside_the_queries(self):
        def work(request):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(0.06)")

        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call(get_response=_view("/api/v2/milestone/7/", work))
        line = _request_lines(cm)[0]
        self.assertEqual(line["db_n"], "1")
        self.assertGreaterEqual(int(line["db_ms"]), 55)
        self.assertGreaterEqual(int(line["wall_ms"]), int(line["db_ms"]))

    def test_cpu_ms_is_this_threads_cpu_not_the_wall_clock(self):
        def sleeps(request):
            time.sleep(0.08)

        def spins(request):
            end = time.thread_time() + 0.05
            while time.thread_time() < end:
                pass

        with self.assertLogs(LOGGER, "INFO") as cm:
            self._call(get_response=_view("/api/v2/milestone/7/", sleeps))
            self._call(get_response=_view("/api/v2/milestone/7/", spins))
        slept, spun = _request_lines(cm)
        self.assertGreaterEqual(int(slept["wall_ms"]), 75)
        self.assertLess(int(slept["cpu_ms"]), 40)
        self.assertGreaterEqual(int(spun["cpu_ms"]), 45)

    def test_queries_on_every_connection_are_wrapped_and_unwrapped(self):
        seen = {}

        def work(request):
            seen.update({alias: list(conn.execute_wrappers) for alias, conn in _all_items()})

        with self.assertLogs(LOGGER, "INFO"):
            self._call(get_response=_view("/api/v2/milestone/7/", work))
        self.assertTrue(seen)
        for alias, wrappers in seen.items():
            self.assertEqual(len(wrappers), 1, alias)
        for _alias, conn in _all_items():
            self.assertEqual(conn.execute_wrappers, [])

    # ── 3. the skips ────────────────────────────────────────────────

    def test_skips_options_health_media_and_static(self):
        skipped = [
            ("options", "/api/v2/milestone/7/"),
            ("get", "/api/v2/health/"),
            ("get", "/api/v2/health/alerting/"),
            ("get", "/media/avatars/a.png"),
            ("get", "/static/admin/app.css"),
        ]
        for method, path in skipped:
            with self.subTest(method=method, path=path), self.assertNoLogs(LOGGER, "INFO"):
                middleware = RequestPerfMiddleware(lambda request: HttpResponse("ok"))
                response = middleware(getattr(self.factory, method)(path))
                self.assertEqual(response.status_code, 200)

    def test_the_skipped_prefixes_are_the_real_urls(self):
        self.assertTrue(reverse("health").startswith(request_perf.HEALTH_PREFIX))
        self.assertTrue(reverse("health_alerting").startswith(request_perf.HEALTH_PREFIX))
        prefixes = request_perf._skip_prefixes()
        self.assertIn(settings.MEDIA_URL, prefixes)
        self.assertIn("/" + settings.STATIC_URL.lstrip("/"), prefixes)

    # ── 4. it never breaks a request ────────────────────────────────

    def test_a_failing_logger_does_not_fail_the_request(self):
        with mock.patch.object(request_perf.log, "info", side_effect=RuntimeError("boom")):
            response = self._call()
        self.assertEqual(response.content, b"ok")

    def test_a_failing_measurement_does_not_fail_the_request(self):
        with mock.patch.object(request_perf, "_Span", side_effect=RuntimeError("boom")):
            response = self._call()
        self.assertEqual(response.content, b"ok")

    def test_a_failing_cpu_sampler_does_not_fail_the_request(self):
        middleware = RequestPerfMiddleware(_view("/api/v2/milestone/7/"))
        middleware._cpu.tick = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs(LOGGER, "INFO"):
            response = middleware(self.factory.get("/api/v2/milestone/7/"))
        self.assertEqual(response.content, b"ok")

    def test_a_raising_view_leaves_no_wrapper_and_no_count(self):
        before = inflight.value

        def explode(request):
            raise RuntimeError("view blew up")

        middleware = RequestPerfMiddleware(explode)
        with self.assertRaises(RuntimeError):
            middleware(self.factory.get("/api/v2/milestone/7/"))
        self.assertEqual(inflight.value, before)
        for _alias, conn in _all_items():
            self.assertEqual(conn.execute_wrappers, [])

    # ── 6. the switch, and where it sits ────────────────────────────

    @override_settings(REQUEST_PERF_LOG=False)
    def test_the_switch_removes_the_middleware(self):
        with self.assertRaises(MiddlewareNotUsed):
            RequestPerfMiddleware(lambda request: HttpResponse("ok"))

    def test_it_sits_right_after_the_request_id(self):
        self.assertEqual(settings.MIDDLEWARE[0], "apis.request_id.RequestIdMiddleware")
        self.assertEqual(settings.MIDDLEWARE[1], MIDDLEWARE_PATH)

    def test_on_by_default(self):
        self.assertIs(settings.REQUEST_PERF_LOG, True)


def _all_items():
    return [(conn.alias, conn) for conn in connections.all()]


class ThroughTheRealStackTests(TestCase):
    """The same middleware, built by Django from `settings.MIDDLEWARE`."""

    def test_an_unauthenticated_request_is_logged_with_its_template(self):
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self.client.get("/api/v2/milestone/123/?secret=abc&page=2")
        self.assertEqual(response.status_code, 401)
        (line,) = _request_lines(cm)
        self.assertEqual(line["route"], "api/v2/milestone/<int:milestone_id>/")
        self.assertEqual(line["status"], "401")
        self.assertEqual(line["method"], "GET")
        self.assertRegex(line["bytes"], r"^\d+$")  # CommonMiddleware sets it
        self.assertNotIn("123", cm.output[0].split("route=")[1])
        self.assertNotIn("secret", cm.output[0])

    def test_a_404_is_unresolved(self):
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self.client.get("/api/v2/definitely/not/a/route/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(_request_lines(cm)[0]["route"], "unresolved")

    def test_health_and_options_are_not_logged(self):
        with self.assertNoLogs(LOGGER, "INFO"):
            self.assertEqual(self.client.get("/api/v2/health/").status_code, 200)
            self.client.options("/api/v2/milestone/123/")

    @override_settings(REQUEST_PERF_LOG=False)
    def test_disabled_logs_nothing_through_the_stack(self):
        with self.assertNoLogs(LOGGER, "INFO"):
            self.assertEqual(self.client.get("/api/v2/milestone/123/").status_code, 401)

    def test_the_request_id_header_still_comes_back(self):
        with self.assertLogs(LOGGER, "INFO"):
            response = self.client.get("/api/v2/milestone/123/")
        self.assertTrue(response["X-Request-ID"])


class InflightTests(SimpleTestCase):
    def test_the_counter_counts_itself_in_and_out(self):
        counter = InflightCounter()
        self.assertEqual([counter.enter(), counter.enter(), counter.enter()], [1, 2, 3])
        counter.leave()
        self.assertEqual(counter.value, 2)
        self.assertEqual(counter.enter(), 3)
        for _ in range(5):
            counter.leave()
        self.assertEqual(counter.value, 0)  # never below zero

    def test_the_counter_is_exact_under_threads(self):
        counter = InflightCounter()
        peak = []

        def hammer():
            for _ in range(2000):
                peak.append(counter.enter())
                counter.leave()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(counter.value, 0)
        self.assertLessEqual(max(peak), 8)

    def _hold_one_open(self, held_path: str):
        """Start a request for `held_path` on another thread and keep it
        inside the stack. Returns (thread, release event, base count)."""
        inside = threading.Event()
        release = threading.Event()
        factory = RequestFactory()

        def slow(request):
            inside.set()
            release.wait(5)
            return HttpResponse("held")

        def run():
            try:
                RequestPerfMiddleware(slow)(factory.get(held_path))
            finally:
                connections.close_all()

        base = inflight.value
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(inside.wait(5))
        self.assertEqual(inflight.value, base + 1)
        return thread, release, base

    def test_overlapping_requests_see_each_other(self):
        """A is still inside the stack when B starts: B logs `inflight=2`,
        A (which started alone) logs `inflight=1`."""
        with self.assertLogs(LOGGER, "INFO") as cm:
            thread, release, base = self._hold_one_open("/api/v2/milestone/1/")
            RequestPerfMiddleware(lambda request: HttpResponse("b"))(
                RequestFactory().get("/api/v2/milestone/2/")
            )
            release.set()
            thread.join(5)
        b_line, a_line = _request_lines(cm)  # B finished first
        self.assertEqual(b_line["inflight"], str(base + 2))
        self.assertEqual(a_line["inflight"], str(base + 1))
        self.assertEqual(inflight.value, base)

    def test_a_skipped_request_still_counts_toward_inflight(self):
        """A `/media/` read is not logged but holds a worker thread, so the
        API request that starts beside it sees `inflight=2`."""
        with self.assertLogs(LOGGER, "INFO") as cm:
            thread, release, base = self._hold_one_open("/media/attachments/a.png")
            RequestPerfMiddleware(lambda request: HttpResponse("b"))(
                RequestFactory().get("/api/v2/milestone/2/")
            )
            release.set()
            thread.join(5)
        (line,) = _request_lines(cm)  # the media read logged nothing
        self.assertEqual(line["inflight"], str(base + 2))
        self.assertEqual(inflight.value, base)


class CpuSamplerTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cpu.stat"
        self.now = 1000.0

    def write(self, *, usage, periods, throttled, throttled_usec):
        self.path.write_text(
            CPU_STAT.format(
                usage=usage, periods=periods, throttled=throttled, throttled_usec=throttled_usec
            )
        )

    def sampler(self, path=None, interval_s=60.0):
        return CpuThrottleSampler(
            self.path if path is None else path, interval_s=interval_s, clock=lambda: self.now
        )

    def test_first_sample_is_only_the_baseline(self):
        self.write(usage=5_000_000, periods=100, throttled=10, throttled_usec=800_000)
        sampler = self.sampler()
        with self.assertNoLogs(LOGGER, "INFO"):
            sampler.tick()
        self.assertFalse(sampler.disabled)

    def test_logs_the_deltas_since_the_last_sample(self):
        self.write(usage=5_000_000, periods=100, throttled=10, throttled_usec=800_000)
        sampler = self.sampler()
        sampler.tick()
        self.write(usage=7_300_000, periods=223, throttled=27, throttled_usec=2_320_000)

        self.now += 30  # not due yet
        with self.assertNoLogs(LOGGER, "INFO"):
            sampler.tick()
        self.now += 30
        with self.assertLogs(LOGGER, "INFO") as cm:
            sampler.tick()
        self.assertEqual(
            cm.output[0].split(":", 2)[2],
            "perf.cpu window_s=60 periods=123 throttled=17 throttled_ms=1520 usage_ms=2300",
        )
        self.assertRegex(cm.output[0].split(":", 2)[2], CPU_LINE)

        # The next window is measured from this sample, not from the first.
        self.write(usage=7_400_000, periods=233, throttled=27, throttled_usec=2_320_000)
        self.now += 75
        with self.assertLogs(LOGGER, "INFO") as cm:
            sampler.tick()
        match = CPU_LINE.match(cm.records[0].getMessage())
        self.assertEqual(
            match.groupdict(),
            {
                "window_s": "75",
                "periods": "10",
                "throttled": "0",
                "throttled_ms": "0",
                "usage_ms": "100",
            },
        )

    def test_counters_that_went_backwards_only_reset_the_baseline(self):
        self.write(usage=9_000_000, periods=500, throttled=50, throttled_usec=9_000_000)
        sampler = self.sampler()
        sampler.tick()
        self.write(usage=1_000, periods=10, throttled=1, throttled_usec=1_000)  # restarted
        self.now += 60
        with self.assertNoLogs(LOGGER, "INFO"):
            sampler.tick()
        self.write(usage=2_001_000, periods=70, throttled=4, throttled_usec=301_000)
        self.now += 60
        with self.assertLogs(LOGGER, "INFO") as cm:
            sampler.tick()
        self.assertIn("periods=60 throttled=3 throttled_ms=300 usage_ms=2000", cm.output[0])

    def test_a_missing_file_turns_it_off_quietly_and_for_good(self):
        sampler = self.sampler(Path(self.path.parent) / "nope" / "cpu.stat")
        with self.assertNoLogs(LOGGER, "INFO"):
            sampler.tick()
        self.assertTrue(sampler.disabled)
        # The file appearing later does not bring it back, and nothing
        # re-reads it on every request.
        self.write(usage=1, periods=1, throttled=0, throttled_usec=0)
        sampler.path = self.path
        with mock.patch("builtins.open", side_effect=AssertionError("read again")):
            self.now += 600
            sampler.tick()
            sampler.tick()

    def test_an_unusable_file_turns_it_off_quietly(self):
        cases = {
            "garbage": "this is not a cgroup file\n",
            "non-numeric": "nr_periods x\nnr_throttled 1\nthrottled_usec 1\nusage_usec 1\n",
            "missing key": "nr_periods 1\nnr_throttled 1\nusage_usec 1\n",
            "empty": "",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.path.write_text(text)
                sampler = self.sampler()
                with self.assertNoLogs(LOGGER, "INFO"):
                    sampler.tick()
                self.assertTrue(sampler.disabled)

    def test_no_path_means_off(self):
        self.assertTrue(self.sampler(path="").disabled)

    def test_the_default_path_is_the_cgroup_v2_file(self):
        self.assertEqual(settings.REQUEST_PERF_CPU_STAT_PATH, "/sys/fs/cgroup/cpu.stat")
        self.assertEqual(request_perf.DEFAULT_CPU_STAT_PATH, "/sys/fs/cgroup/cpu.stat")

    def test_only_one_thread_samples_at_a_time(self):
        self.write(usage=1, periods=1, throttled=0, throttled_usec=0)
        sampler = self.sampler()
        sampler.tick()  # baseline
        self.write(usage=2_000_000, periods=61, throttled=3, throttled_usec=100_000)
        self.now += 60
        sampler._lock.acquire()  # another thread is mid-sample
        try:
            with self.assertNoLogs(LOGGER, "INFO"):
                sampler.tick()
        finally:
            sampler._lock.release()
        with self.assertLogs(LOGGER, "INFO"):
            sampler.tick()


class SamplerThroughTheMiddlewareTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cpu.stat"
        self.now = 5000.0
        self.factory = RequestFactory()

    def build(self):
        middleware = RequestPerfMiddleware(_view("/api/v2/milestone/7/"))
        middleware._cpu.clock = lambda: self.now
        return middleware

    def get(self, middleware):
        return middleware(self.factory.get("/api/v2/milestone/7/"))

    def test_the_cpu_line_follows_the_request_line_once_a_minute(self):
        self.path.write_text(CPU_STAT.format(usage=0, periods=0, throttled=0, throttled_usec=0))
        with override_settings(REQUEST_PERF_CPU_STAT_PATH=str(self.path)):
            middleware = self.build()
        with self.assertLogs(LOGGER, "INFO") as cm:
            self.get(middleware)  # baseline
            self.get(middleware)  # same minute
            self.path.write_text(
                CPU_STAT.format(
                    usage=2_300_000, periods=123, throttled=17, throttled_usec=1_520_000
                )
            )
            self.now += 60
            self.get(middleware)
            self.get(middleware)
        messages = [r.getMessage() for r in cm.records]
        self.assertEqual(
            [m.split(" ")[0] for m in messages],
            ["perf.request", "perf.request", "perf.request", "perf.cpu", "perf.request"],
        )
        self.assertEqual(
            messages[3],
            "perf.cpu window_s=60 periods=123 throttled=17 throttled_ms=1520 usage_ms=2300",
        )

    def test_a_missing_file_leaves_the_request_lines_alone(self):
        with override_settings(REQUEST_PERF_CPU_STAT_PATH=str(self.path.parent / "gone")):
            middleware = self.build()
        with self.assertLogs(LOGGER, "INFO") as cm:
            for _ in range(3):
                self.now += 60
                self.assertEqual(self.get(middleware).status_code, 200)
        self.assertEqual(len(_request_lines(cm)), 3)
        self.assertEqual(_messages(cm, "perf.cpu"), [])

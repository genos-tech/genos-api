"""`POST /api/v2/client-perf/`, the browser's interaction timings (MIP-957).

What's pinned, roughly in order of how silently each would break:

1. **The log line.** Someone parses `perf.client`: its keys, their order and
   `-` for a missing value are a contract, and one accepted sample is
   exactly one line on `origin.perf`.
2. **Nothing but the sample's own fields reaches the log,** and nothing
   reaches the database: no user, team, path or free text, and no value a
   client can use to forge a second line.
3. **Bad input is dropped, not fatal.** An unknown kind, a negative or
   non-numeric number, a sample that is not an object: counted in
   `dropped`, never a 400 or a 500. Whole-request problems (too many
   samples, not an object, bad JSON, too big) are a 400.
4. **Sign-in and the rate limit.** 401 without a token; 429 past the
   per-user limit, which one user's traffic cannot spend for another.
"""

from __future__ import annotations

import json
import logging
import os
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from origin.services import throttles
from origin.services.throttles import (
    ClientPerfThrottle,
    UserBurstThrottle,
    UserSustainedThrottle,
    parse_rate,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.client_perf_views import (
    KINDS,
    MAX_BODY_BYTES,
    MAX_SAMPLES,
    ClientPerfView,
)

# The class's own rate, before `ClientPerfCase.setUp` raises it for the loops.
DEFAULT_RATE = ClientPerfThrottle.rate

URL = "/api/v2/client-perf/"
LOGGER = "origin.perf"

SAMPLE = {
    "kind": "task_switch",
    "paint_ms": 120,
    "settle_ms": 1500,
    "blocked_ms": 900,
    "longest_ms": 250,
    "requests": 11,
    "window_ms": 3200,
}
CTX = {"cores": 8, "homes_mounted": 2, "viewport_w": 1500, "build": "abc1234"}

# The contract's example, character for character.
EXAMPLE_LINE = (
    "perf.client kind=task_switch paint_ms=120 settle_ms=1500 blocked_ms=900 "
    "longest_ms=250 requests=11 window_ms=3200 cores=8 homes=2 vw=1500 build=abc1234"
)


def _records(cm) -> list[logging.LogRecord]:
    """Only the `perf.client` records: the same logger carries the server's
    own `perf.request` line for every request once MIP-956's middleware is
    in, and these tests are about what THIS endpoint writes."""
    return [r for r in cm.records if r.getMessage().startswith("perf.client")]


def _lines(cm) -> list[str]:
    return [r.getMessage() for r in _records(cm)]


def _sample(**changes):
    return {**SAMPLE, **changes}


class ClientPerfCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The limit counts in fixed windows of the real clock; freeze the
        # one the limiter reads at mid-minute so a test that sends three
        # requests under `2/min` cannot straddle a minute boundary (the
        # same fix `test_throttles` makes).
        self.now = time.time() // 3600 * 3600 + 1830
        fake = SimpleNamespace(time=lambda: self.now, monotonic=time.monotonic)
        patcher = mock.patch.object(throttles, "time", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real 30/min would refuse the loops below, which send dozens of
        # requests as one user; the tests of the limit itself lower it.
        roomy = mock.patch.object(ClientPerfThrottle, "rate", "100000/min")
        roomy.start()
        self.addCleanup(roomy.stop)
        self.authenticate()

    def post(self, body, **extra):
        return self.client.post(URL, body, format="json", **extra)

    def post_raw(self, text: str):
        return self.client.post(URL, data=text, content_type="application/json")

    def logged(self, body) -> tuple[list[str], object]:
        """POST `body`, which must be accepted; returns (log lines, response)."""
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self.post(body)
        self.assertEqual(response.status_code, 202, response.content)
        return _lines(cm), response

    @contextmanager
    def no_client_lines(self):
        """Nothing is logged as `perf.client` inside the block. Not
        `assertNoLogs`: another middleware may log other lines on this
        logger for the same request. A sentinel gives `assertLogs` the one
        record it insists on."""
        with self.assertLogs(LOGGER, "INFO") as cm:
            logging.getLogger(LOGGER).info("sentinel: assertLogs needs a record")
            yield
        self.assertEqual(_lines(cm), [])

    def nothing_logged(self, body_or_raw, *, raw=False):
        with self.no_client_lines():
            return self.post_raw(body_or_raw) if raw else self.post(body_or_raw)


class TestTheLogLine(ClientPerfCase):
    def test_the_contract_example_is_logged_character_for_character(self):
        lines, response = self.logged({"samples": [SAMPLE], "ctx": CTX})
        self.assertEqual(lines, [EXAMPLE_LINE])
        self.assertEqual(response.json(), {"accepted": 1, "dropped": 0})

    def test_one_line_per_accepted_sample_at_info_on_origin_perf(self):
        samples = [_sample(kind="task_open"), _sample(kind="note_switch", paint_ms=5)]
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self.post({"samples": samples, "ctx": CTX})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(_records(cm)), 2)
        for record in _records(cm):
            self.assertEqual(record.name, LOGGER)
            self.assertEqual(record.levelname, "INFO")
        self.assertTrue(_lines(cm)[0].startswith("perf.client kind=task_open paint_ms=120 "))
        self.assertTrue(_lines(cm)[1].startswith("perf.client kind=note_switch paint_ms=5 "))

    def test_every_allowed_kind_is_accepted(self):
        self.assertEqual(
            KINDS,
            {
                "task_open",
                "task_switch",
                "note_open",
                "note_switch",
                "project_switch",
                "service_switch",
                "idle_minute",
            },
        )
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                lines, _ = self.logged({"samples": [_sample(kind=kind)]})
                self.assertEqual(len(lines), 1)
                self.assertIn(f"kind={kind} ", lines[0])

    def test_no_ctx_logs_dashes_for_the_ctx_fields(self):
        lines, _ = self.logged({"samples": [SAMPLE]})
        self.assertEqual(
            lines,
            [
                EXAMPLE_LINE.replace(
                    "cores=8 homes=2 vw=1500 build=abc1234", "cores=- homes=- vw=- build=-"
                )
            ],
        )

    def test_null_and_missing_numbers_are_dashes(self):
        lines, _ = self.logged(
            {
                "samples": [
                    {"kind": "idle_minute", "paint_ms": None, "blocked_ms": None, "requests": 4}
                ]
            }
        )
        self.assertEqual(
            lines,
            [
                "perf.client kind=idle_minute paint_ms=- settle_ms=- blocked_ms=- longest_ms=- "
                "requests=4 window_ms=- cores=- homes=- vw=- build=-"
            ],
        )

    def test_a_ctx_that_is_not_an_object_is_ignored(self):
        for ctx in ("x", 5, [1, 2], None, True):
            with self.subTest(ctx=ctx):
                lines, response = self.logged({"samples": [SAMPLE], "ctx": ctx})
                self.assertTrue(lines[0].endswith("cores=- homes=- vw=- build=-"))
                self.assertEqual(response.json()["accepted"], 1)

    def test_an_empty_batch_is_accepted(self):
        with self.no_client_lines():
            response = self.post({"samples": []})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": 0, "dropped": 0})


class TestDroppedNotFatal(ClientPerfCase):
    def test_an_unknown_kind_is_dropped_and_the_rest_kept(self):
        lines, response = self.logged(
            {"samples": [_sample(kind="teleport"), _sample(kind="task_open")], "ctx": CTX}
        )
        self.assertEqual(response.json(), {"accepted": 1, "dropped": 1})
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=task_open ", lines[0])

    def test_a_kind_that_is_not_an_allowed_string_is_dropped(self):
        for kind in ("TASK_SWITCH", "task_switch ", "", None, 5, ["task_open"], {"a": 1}, True):
            with self.subTest(kind=kind):
                response = self.nothing_logged({"samples": [_sample(kind=kind)]})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json(), {"accepted": 0, "dropped": 1})
        with self.no_client_lines():
            response = self.post({"samples": [{"paint_ms": 5}]})
        self.assertEqual(response.json(), {"accepted": 0, "dropped": 1})

    def test_a_negative_or_non_numeric_number_drops_that_sample(self):
        bad = [-1, -0.4, "120", "", True, False, [1], {"a": 1}]
        for field in ("paint_ms", "settle_ms", "blocked_ms", "longest_ms", "requests", "window_ms"):
            for value in bad:
                with self.subTest(field=field, value=value):
                    response = self.nothing_logged({"samples": [_sample(**{field: value})]})
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json(), {"accepted": 0, "dropped": 1})

    def test_a_number_json_reads_as_infinity_drops_the_sample(self):
        # `1e999` is valid JSON that Python reads as inf; `round(inf)` raises.
        raw = (
            '{"samples":[{"kind":"task_open","paint_ms":1e999},{"kind":"task_open","paint_ms":7}]}'
        )
        with self.assertLogs(LOGGER, "INFO") as cm:
            response = self.post_raw(raw)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": 1, "dropped": 1})
        self.assertIn("paint_ms=7 ", _lines(cm)[0])

    def test_nan_and_infinity_literals_are_a_bad_request(self):
        # Not JSON; DRF's parser refuses them before the view sees them.
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                response = self.nothing_logged(
                    '{"samples":[{"kind":"task_open","paint_ms":%s}]}' % literal, raw=True
                )
                self.assertEqual(response.status_code, 400)

    def test_a_sample_that_is_not_an_object_is_dropped(self):
        samples = [1, "task_open", None, [], [SAMPLE], True, _sample(kind="task_open")]
        lines, response = self.logged({"samples": samples})
        self.assertEqual(response.json(), {"accepted": 1, "dropped": 6})
        self.assertEqual(len(lines), 1)

    def test_extra_keys_are_ignored_and_never_logged(self):
        sample = _sample(user_id=77, team_id=9, email="a@b.co", note="free text", url="/x?y=1")
        lines, _ = self.logged(
            {"samples": [sample], "ctx": {**CTX, "user": "ken", "path": "/p"}, "token": "t"}
        )
        self.assertEqual(lines, [EXAMPLE_LINE])


class TestClampingAndRounding(ClientPerfCase):
    def test_floats_are_rounded_to_whole_numbers(self):
        lines, _ = self.logged(
            {"samples": [_sample(paint_ms=120.6, settle_ms=0.4, blocked_ms=7.0, requests=2.51)]}
        )
        self.assertIn("paint_ms=121 settle_ms=0 blocked_ms=7 longest_ms=250 requests=3 ", lines[0])

    def test_sample_numbers_are_clamped_to_600000(self):
        lines, _ = self.logged(
            {
                "samples": [
                    _sample(
                        paint_ms=600000,
                        settle_ms=600001,
                        blocked_ms=10**12,
                        longest_ms=1e300,
                        requests=10**40,
                        window_ms=599999.4,
                    )
                ]
            }
        )
        self.assertIn(
            "paint_ms=600000 settle_ms=600000 blocked_ms=600000 longest_ms=600000 "
            "requests=600000 window_ms=599999 ",
            lines[0],
        )

    def test_zero_is_a_value_not_a_dash(self):
        lines, _ = self.logged({"samples": [_sample(paint_ms=0, requests=0)]})
        self.assertIn("paint_ms=0 settle_ms=1500 ", lines[0])
        self.assertIn(" requests=0 ", lines[0])

    def test_ctx_numbers_are_clamped(self):
        cases = [
            (
                {"cores": 9999, "homes_mounted": 99, "viewport_w": 999999},
                "cores=256 homes=3 vw=20000",
            ),
            ({"cores": 256, "homes_mounted": 3, "viewport_w": 20000}, "cores=256 homes=3 vw=20000"),
            ({"cores": -4, "homes_mounted": -1, "viewport_w": -1500}, "cores=0 homes=0 vw=0"),
            ({"cores": 7.6, "homes_mounted": 1.2, "viewport_w": 1440.5}, "cores=8 homes=1 vw=1440"),
        ]
        for ctx, expected in cases:
            with self.subTest(ctx=ctx):
                lines, _ = self.logged({"samples": [SAMPLE], "ctx": ctx})
                self.assertIn(f" {expected} build=-", lines[0])

    def test_a_bad_ctx_number_costs_only_that_field(self):
        ctx = {"cores": "8", "homes_mounted": True, "viewport_w": [1], "build": "ok1"}
        lines, response = self.logged({"samples": [SAMPLE], "ctx": ctx})
        self.assertEqual(response.json()["accepted"], 1)
        self.assertTrue(lines[0].endswith("cores=- homes=- vw=- build=ok1"))


class TestBuildIsSanitised(ClientPerfCase):
    def build_of(self, build) -> str:
        lines, _ = self.logged({"samples": [SAMPLE], "ctx": {"build": build}})
        self.assertEqual(len(lines), 1)
        self.assertNotIn("\n", lines[0])
        return lines[0].rsplit(" build=", 1)[1]

    def test_only_letters_digits_dot_underscore_and_hyphen_survive(self):
        self.assertEqual(self.build_of("v1.2.3-rc_4"), "v1.2.3-rc_4")
        self.assertEqual(self.build_of("abc 123;rm -rf /"), "abc123rm-rf")
        self.assertEqual(self.build_of("a=b&c=d"), "abcd")
        self.assertEqual(self.build_of("ビルド-7"), "-7")
        self.assertEqual(self.build_of("é"), "-")  # nothing left

    def test_at_most_40_characters_of_what_is_left(self):
        self.assertEqual(self.build_of("a" * 100), "a" * 40)
        self.assertEqual(self.build_of("a" * 40), "a" * 40)
        self.assertEqual(self.build_of("!" * 100 + "b" * 100), "b" * 40)

    def test_it_cannot_forge_a_second_line(self):
        build = "x\nperf.request method=GET route=evil status=200\r\nperf.client kind=task_open"
        with self.assertLogs(LOGGER, "INFO") as cm:
            self.post({"samples": [SAMPLE], "ctx": {"build": build}})
        (message,) = _lines(cm)
        self.assertEqual(message.count(" kind="), 1)
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertNotIn(" method=", message)
        self.assertNotIn("route=", message)

    def test_nothing_usable_or_not_a_string_is_a_dash(self):
        for build in ("", "   ", "@@@", 12345, ["abc"], {"a": "b"}, True, None):
            with self.subTest(build=build):
                self.assertEqual(self.build_of(build), "-")


class TestWholeRequestErrors(ClientPerfCase):
    def test_exactly_the_limit_is_accepted_one_more_is_a_400(self):
        lines, response = self.logged({"samples": [SAMPLE] * MAX_SAMPLES})
        self.assertEqual(len(lines), 20)
        self.assertEqual(response.json(), {"accepted": 20, "dropped": 0})

        with self.no_client_lines():
            response = self.post({"samples": [SAMPLE] * (MAX_SAMPLES + 1)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_more_than_the_limit_of_junk_is_still_a_400_not_20_drops(self):
        response = self.nothing_logged({"samples": [1] * 21})
        self.assertEqual(response.status_code, 400)

    def test_a_body_that_is_not_a_json_object_is_a_400(self):
        bodies = ["[]", "[1,2]", '"samples"', "5", "null", "true", '[{"samples": []}]']
        for raw in bodies:
            with self.subTest(body=raw):
                self.assertEqual(self.nothing_logged(raw, raw=True).status_code, 400)

    def test_samples_must_be_a_list(self):
        for body in (
            {},
            {"samples": None},
            {"samples": "x"},
            {"samples": {}},
            {"samples": 3},
            {"ctx": CTX},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.nothing_logged(body).status_code, 400)

    def test_malformed_json_is_a_400(self):
        for raw in ("{", '{"samples": [}', "not json", ""):
            with self.subTest(body=raw):
                self.assertEqual(self.nothing_logged(raw, raw=True).status_code, 400)

    def test_a_body_over_8kb_is_a_400_and_logs_nothing(self):
        body = {"samples": [SAMPLE], "ctx": {"build": "a" * (MAX_BODY_BYTES + 1)}}
        self.assertGreater(len(json.dumps(body)), MAX_BODY_BYTES)
        response = self.nothing_logged(body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_a_body_just_under_8kb_is_accepted(self):
        body = {"samples": [SAMPLE], "ctx": {"build": "a" * 8000}}
        self.assertLess(len(json.dumps(body)), MAX_BODY_BYTES)
        lines, _ = self.logged(body)
        self.assertTrue(lines[0].endswith("build=" + "a" * 40))

    def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        # Declared length, not the parsed size: the cap holds for a body
        # that would not even parse, and nothing asks DRF to parse it.
        with mock.patch("rest_framework.request.Request._load_data_and_files") as parse:
            response = self.client.post(
                URL, data="{" + " " * (MAX_BODY_BYTES + 10), content_type="application/json"
            )
        self.assertEqual(response.status_code, 400)
        parse.assert_not_called()

    def test_only_json_and_only_post(self):
        self.assertEqual(self.client.post(URL, {"samples": []}).status_code, 415)  # multipart
        self.assertEqual(
            self.client.post(
                URL, "samples=1", content_type="application/x-www-form-urlencoded"
            ).status_code,
            415,
        )
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(URL).status_code, 405)


class TestSignInAndRateLimit(ClientPerfCase):
    def test_unauthenticated_is_401_and_logs_nothing(self):
        self.unauthenticate()
        with self.no_client_lines():
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 401)

    def test_a_bad_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
        with self.no_client_lines():
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 401)

    def test_429_after_the_limit_and_nothing_is_logged_for_it(self):
        with mock.patch.object(ClientPerfThrottle, "rate", "2/min"):
            self.assertEqual(self.logged({"samples": [SAMPLE]})[1].status_code, 202)
            self.assertEqual(self.logged({"samples": [SAMPLE]})[1].status_code, 202)
            with (
                self.no_client_lines(),
                self.assertLogs("origin.services.throttles", "WARNING") as denied,
            ):
                response = self.post({"samples": [SAMPLE]})
        self.assertEqual(response.status_code, 429)
        self.assertIn("throttle.denied scope=client_perf", denied.output[0])

    def test_the_limit_is_per_user(self):
        with mock.patch.object(ClientPerfThrottle, "rate", "1/min"):
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 202)
            with self.assertLogs("origin.services.throttles", "WARNING"):
                self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 429)
            self.authenticate(self.user2)
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 202)

    def test_the_limit_resets_with_the_window(self):
        with mock.patch.object(ClientPerfThrottle, "rate", "1/min"):
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 202)
            with self.assertLogs("origin.services.throttles", "WARNING"):
                self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 429)
            self.now += 60
            self.assertEqual(self.post({"samples": [SAMPLE]}).status_code, 202)

    def test_it_has_its_own_scope_and_keeps_the_default_backstop(self):
        classes = ClientPerfView.throttle_classes
        self.assertEqual(ClientPerfThrottle.scope, "client_perf")
        self.assertIn(ClientPerfThrottle, classes)
        self.assertIn(UserBurstThrottle, classes)
        self.assertIn(UserSustainedThrottle, classes)
        parse_rate(DEFAULT_RATE)

    @unittest.skipIf(os.environ.get("THROTTLE_CLIENT_PERF"), "the rate is overridden here")
    def test_the_default_rate_is_30_a_minute(self):
        self.assertEqual(parse_rate(DEFAULT_RATE), (30, 60))


class TestWhatItTouches(ClientPerfCase):
    def test_it_writes_nothing_to_the_database(self):
        with CaptureQueriesContext(connection) as queries:
            self.logged({"samples": [SAMPLE, _sample(kind="nope")], "ctx": CTX})
        writes = [
            q["sql"]
            for q in queries.captured_queries
            if not q["sql"].lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE"))
        ]
        self.assertEqual(writes, [])

    def test_a_team_or_project_in_the_body_is_not_looked_up(self):
        body = {"samples": [SAMPLE], "team_id": str(self.team.team_id), "project_id": 12345}
        lines, response = self.logged(body)
        self.assertEqual(response.json(), {"accepted": 1, "dropped": 0})
        self.assertEqual(len(lines), 1)

    def test_the_line_holds_nothing_about_the_caller(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.get_access_token()}",
            HTTP_X_FORWARDED_FOR="203.0.113.9",
            HTTP_USER_AGENT="SecretBrowser/1.0",
        )
        with self.assertLogs(LOGGER, "INFO") as cm:
            self.post({"samples": [SAMPLE], "ctx": CTX})
        (record,) = _records(cm)
        self.assertEqual(record.getMessage(), EXAMPLE_LINE)
        everything = record.name + record.getMessage()
        for leaked in (
            self.user.username,
            self.user.email,
            str(self.team.team_id),
            "203.0.113.9",
            "SecretBrowser",
            "Bearer",
            "client-perf",
        ):
            self.assertNotIn(leaked, everything)


class TestRouting(ClientPerfCase):
    def test_the_route(self):
        self.assertEqual(reverse("client_perf"), URL)
        self.assertIs(resolve(URL).func.view_class, ClientPerfView)

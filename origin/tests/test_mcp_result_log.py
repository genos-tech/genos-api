"""Tool name and answer size in the `perf.request` line, and the shadow cap (MIP-1367 phase 1,
`views/public/mcp/result_log.py`).

Observation only: nothing here may change what a client receives. The twin tests prove it by
calling the same tool with the logging on and with it switched off and comparing the bytes.
"""

from __future__ import annotations

import json
import re
from unittest import mock

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from origin.middleware import request_perf
from origin.views.public.mcp import result_log

from .test_mcp_endpoint import McpBase

PERF = "origin.perf"
LOGGER = "origin.views.public.mcp.result_log"
LINE = re.compile(
    r"^perf\.request method=POST route=\S+ status=(?P<status>\d+) wall_ms=\d+ cpu_ms=\d+ "
    r"db_n=\d+ db_ms=\d+ inflight=\d+ bytes=(?P<bytes>\d+|-)"
    r"(?: tool=(?P<tool>[A-Za-z0-9_.-]+) result_chars=(?P<chars>\d+))?$"
)
SECRET = "marker-argument-value"


def _lines(cm):
    out = []
    for record in cm.records:
        msg = record.getMessage()
        if msg.startswith("perf.request"):
            match = LINE.match(msg)
            assert match, f"not in the perf.request shape: {msg!r}"
            out.append(match.groupdict())
    return out


class PerfLine(McpBase):
    def setUp(self):
        super().setUp()
        self._key()

    def test_a_tool_call_adds_its_name_and_the_size_of_its_text(self):
        with self.assertLogs(PERF, "INFO") as cm:
            res = self._call("get_current_user")
        (line,) = _lines(cm)
        text = res.json()["result"]["content"][0]["text"]
        self.assertEqual(line["tool"], "get_current_user")
        self.assertEqual(int(line["chars"]), len(text))
        # the wire bytes were already on the line, and are the response's
        self.assertEqual(int(line["bytes"]), len(res.content))

    def test_a_tool_that_refuses_is_measured_too(self):
        with self.assertLogs(PERF, "INFO") as cm:
            res = self._call("get_task", {"task_id": SECRET})
        (line,) = _lines(cm)
        self.assertTrue(res.json()["result"]["isError"])
        self.assertEqual(line["tool"], "get_task")
        self.assertEqual(int(line["chars"]), len(res.json()["result"]["content"][0]["text"]))

    def test_other_requests_keep_the_old_shape(self):
        for method in ("ping", "tools/list"):
            with self.assertLogs(PERF, "INFO") as cm:
                self._rpc(method)
            (line,) = _lines(cm)
            self.assertIsNone(line["tool"], method)
            self.assertIsNone(line["chars"], method)

    def test_an_unknown_tool_is_a_protocol_error_and_not_a_measurement(self):
        with self.assertLogs(PERF, "INFO") as cm:
            res = self._call("no_such_tool")
        (line,) = _lines(cm)
        self.assertIn("error", res.json())
        self.assertIsNone(line["tool"])

    def test_a_tool_name_the_client_invented_never_reaches_the_log(self):
        with self.assertLogs(PERF, "INFO") as cm:
            self._call("evil\nperf.request method=GET status=200 " + SECRET)
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertNotIn(SECRET, text)
        self.assertEqual(len(_lines(cm)), 1)

    def test_no_argument_and_no_result_content_is_logged(self):
        with self.assertLogs("origin", "INFO") as cm:
            with mock.patch.object(result_log, "MCP_RESULT_SHADOW_CAP_CHARS", 10):
                self._call("get_task", {"task_id": SECRET})
                self._call("get_current_user")
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(self.user.email, text)


class ShadowCap(McpBase):
    def setUp(self):
        super().setUp()
        self._key()

    def result_chars(self):
        res = self._call("get_current_user")
        return len(res.json()["result"]["content"][0]["text"])

    def test_the_default_is_the_proposed_24000(self):
        self.assertEqual(result_log.DEFAULT_SHADOW_CAP_CHARS, 24_000)
        self.assertEqual(result_log.MCP_RESULT_SHADOW_CAP_CHARS, 24_000)

    def test_over_the_cap_one_line_and_nothing_else_changes(self):
        n = self.result_chars()
        with mock.patch.object(result_log, "MCP_RESULT_SHADOW_CAP_CHARS", n - 1):
            with self.assertLogs(LOGGER, "INFO") as cm:
                over = self._call("get_current_user")
        self.assertEqual(
            [r.getMessage() for r in cm.records],
            [f"mcp.result_over_shadow_cap tool=get_current_user result_chars={n} cap={n - 1}"],
        )
        self.assertEqual(over.status_code, 200)
        self.assertFalse(over.json()["result"]["isError"])
        self.assertEqual(len(over.json()["result"]["content"][0]["text"]), n)

    def test_at_the_cap_or_under_it_there_is_no_line(self):
        n = self.result_chars()
        for cap in (n, n + 1, 24_000):
            with mock.patch.object(result_log, "MCP_RESULT_SHADOW_CAP_CHARS", cap):
                with self.assertNoLogs(LOGGER, "INFO"):
                    self._call("get_current_user")

    def test_a_result_over_the_real_default_is_logged_and_still_returned_whole(self):
        # a read that really is big: a note body of ~60,000 characters
        from origin.models.note.personal_note_models import PersonalNoteMaster

        para = {
            "id": "b",
            "type": "paragraph",
            "props": {},
            "content": [{"type": "text", "text": "x" * 100, "styles": {}}],
            "children": [],
        }
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Big",
            body=[dict(para, id=f"b{i}") for i in range(600)],
        )
        with self.assertLogs(LOGGER, "INFO") as cm:
            res = self._call("get_note", {"note_id": note.note_id, "note_type": "personal"})
        text = res.json()["result"]["content"][0]["text"]
        self.assertGreater(len(text), 24_000)
        (message,) = [r.getMessage() for r in cm.records]
        self.assertEqual(
            message, f"mcp.result_over_shadow_cap tool=get_note result_chars={len(text)} cap=24000"
        )
        self.assertEqual(
            res.json()["result"]["structuredContent"]["body_markdown"].count("x"), 60_000
        )

    def test_env_values_that_are_not_a_positive_number_keep_the_default(self):
        for raw, want in (
            ("", 24_000),
            ("x", 24_000),
            ("0", 24_000),
            ("-1", 24_000),
            ("5000", 5000),
        ):
            with mock.patch.dict("os.environ", {"MCP_RESULT_SHADOW_CAP_CHARS": raw}):
                self.assertEqual(result_log._env_int("MCP_RESULT_SHADOW_CAP_CHARS", 24_000), want)


class TwinOfTheAnswerWithoutTheLog(McpBase):
    """The logging is the only thing this change adds to a call, so the same calls with it
    switched off must produce byte-identical answers."""

    def calls(self):
        out = []
        for name, args in (
            ("get_current_user", {}),
            ("list_projects", {}),
            ("get_task", {"task_id": self.task.task_id}),
            ("get_task", {"task_id": "nope"}),
            ("list_my_tasks", {}),
            ("get_current_user", {"bogus": 1}),
        ):
            res = self._call(name, args)
            out.append((res.status_code, res.content))
        out.append((lambda r: (r.status_code, r.content))(self._rpc("tools/list")))
        return out

    def test_every_answer_is_identical_with_and_without_the_logging(self):
        self._key()
        with_log = self.calls()
        with mock.patch("origin.views.public.mcp.views.note_result"):
            without = self.calls()
        self.assertEqual(with_log, without)

    def test_a_shadow_over_the_cap_does_not_touch_the_answer_either(self):
        self._key()
        quiet = self.calls()
        with mock.patch.object(result_log, "MCP_RESULT_SHADOW_CAP_CHARS", 1):
            loud = self.calls()
        self.assertEqual(quiet, loud)

    def test_a_failure_in_the_logging_cannot_fail_a_call(self):
        self._key()
        baseline = self._call("get_current_user")
        with mock.patch.object(result_log, "text_chars", side_effect=RuntimeError("boom")):
            res = self._call("get_current_user")
        self.assertEqual((res.status_code, res.content), (baseline.status_code, baseline.content))
        self.assertTrue(json.loads(res.content)["result"]["content"])


class Units(SimpleTestCase):
    def test_text_chars_sums_the_text_items_only(self):
        result = {
            "content": [
                {"type": "text", "text": "abc"},
                {"type": "image", "data": "zzzz"},
                {"type": "text", "text": "de"},
            ],
            "structuredContent": {"big": "x" * 1000},
        }
        self.assertEqual(result_log.text_chars(result), 5)
        self.assertEqual(result_log.text_chars({}), 0)
        self.assertEqual(result_log.text_chars({"content": None}), 0)

    def test_text_chars_counts_characters_not_bytes(self):
        self.assertEqual(
            result_log.text_chars({"content": [{"type": "text", "text": "日本語"}]}), 3
        )

    def test_note_result_never_raises(self):
        request = RequestFactory().post("/")
        for tool, result in (
            ("t", None),
            ("t", {"content": 5}),
            (None, {}),
            ("a b", {}),
            ("t", {"content": [None]}),
        ):
            result_log.note_result(request, tool, result)  # type: ignore[arg-type]

    def test_note_result_stashes_on_the_django_request_under_a_drf_one(self):
        inner = RequestFactory().post("/")
        outer = mock.Mock(_request=inner)
        result_log.note_result(outer, "get_task", {"content": [{"type": "text", "text": "abcd"}]})
        self.assertEqual((inner.mcp_tool, inner.mcp_result_chars), ("get_task", 4))

    def test_a_name_that_is_not_shaped_like_a_tool_is_not_stashed(self):
        request = RequestFactory().post("/")
        for bad in ("a\nb", "a b", "", "x" * 65, "t=1"):
            result_log.note_result(request, bad, {"content": []})
        self.assertFalse(hasattr(request, "mcp_tool"))


class Middleware(SimpleTestCase):
    def line(self, **attrs):
        def get_response(request):
            request.resolver_match = None
            for key, value in attrs.items():
                setattr(request, key, value)
            return HttpResponse("ok")

        with self.assertLogs(PERF, "INFO") as cm:
            request_perf.RequestPerfMiddleware(get_response)(
                RequestFactory().post("/api/public/v1/mcp")
            )
        return [r.getMessage() for r in cm.records if r.getMessage().startswith("perf.request")][0]

    def test_the_tool_fields_come_after_bytes(self):
        line = self.line(mcp_tool="get_task", mcp_result_chars=123)
        self.assertTrue(line.endswith(" bytes=- tool=get_task result_chars=123"), line)

    def test_without_them_the_line_is_exactly_what_it_was(self):
        self.assertTrue(self.line().endswith(" bytes=-"))

    def test_a_malformed_stash_adds_nothing(self):
        self.assertTrue(self.line(mcp_tool="get_task").endswith(" bytes=-"))
        self.assertTrue(self.line(mcp_tool=5, mcp_result_chars=3).endswith(" bytes=-"))
        self.assertTrue(self.line(mcp_tool="t", mcp_result_chars="3").endswith(" bytes=-"))

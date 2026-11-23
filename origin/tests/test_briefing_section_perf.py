"""Per-section query counts of `get_my_briefing` on the perf line (MIP-2179)."""

from __future__ import annotations

import json
import re
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.services import briefing, section_perf
from origin.tests.test_briefing import BriefingBase

SECTIONS = re.compile(r" sections=(\S+)")


class SectionPerfTests(BriefingBase):
    def _run(self):
        """One MCP call through the perf middleware; returns (result text, perf line)."""
        with self.assertLogs("origin.perf", level="INFO") as logs:
            with CaptureQueriesContext(connection) as queries:
                text, is_error = self._tool_text(self._call("get_my_briefing", {}))
        self.assertFalse(is_error, text)
        line = next(m for m in logs.output if "perf.request" in m and "tool=get_my_briefing" in m)
        return text, line, len(queries)

    def test_counts_add_up_to_the_queries_of_the_build(self):
        self.make_task("one", assignee=self.user)
        with mock.patch.dict("os.environ", {"AGENT_CALL_LOG": "1"}):
            with CaptureQueriesContext(connection) as queries:
                briefing.build_briefing(self.ctx, now=self.now)
            summary = section_perf.take()
        match = SECTIONS.search(summary)
        self.assertIsNotNone(match, summary)
        entries = dict(e.split(":") for e in match.group(1).split(","))
        counted = sum(int(v.split("/")[0]) for v in entries.values())
        self.assertGreater(counted, 0)
        # Everything the build queries is inside a section (the sections cover it), so the
        # entries add up to what the queries log saw.
        self.assertEqual(counted, len(queries))
        for name in ("setup", "changed_for_me", "agenda", "waiting_on_me", "ranking"):
            self.assertIn(name, entries)

    def test_the_line_carries_the_sections_when_the_log_is_on(self):
        self.make_task("one", assignee=self.user)
        with mock.patch.dict("os.environ", {"AGENT_CALL_LOG": "1"}):
            _text, line, _n = self._run()
        self.assertRegex(line, r"tool=get_my_briefing result_chars=\d+ sections=setup:\d+/\d+")
        # Names and numbers only.
        tail = line.split(" sections=", 1)[1].split()[0]
        self.assertRegex(tail, r"^([a-z_]+:\d+/\d+)(,[a-z_]+:\d+/\d+)*$")

    def test_nothing_is_logged_or_wrapped_when_the_log_is_off(self):
        env = {"AGENT_CALL_LOG": "0"}
        with mock.patch.dict("os.environ", env):
            _text, line, _n = self._run()
        self.assertNotIn("sections=", line)
        with mock.patch.dict("os.environ", env):
            with mock.patch.object(section_perf, "_Collector") as collector:
                briefing.build_briefing(self.ctx, now=self.now)
        collector.assert_not_called()
        self.assertEqual(section_perf.take(), "")

    def test_the_result_is_the_same_with_the_log_on_and_off(self):
        self.make_task("one", assignee=self.user)

        def build():
            payload = briefing.build_briefing(self.ctx, now=self.now, window="today")
            return json.dumps(payload, sort_keys=True, default=str)

        with mock.patch.dict("os.environ", {"AGENT_CALL_LOG": "0"}):
            off = build()
        with mock.patch.dict("os.environ", {"AGENT_CALL_LOG": "1"}):
            on = build()
        section_perf.reset()
        self.assertEqual(off, on)

    def test_a_summary_is_taken_once(self):
        with mock.patch.dict("os.environ", {"AGENT_CALL_LOG": "1"}):
            briefing.build_briefing(self.ctx, now=self.now)
        self.assertIn(" sections=", section_perf.take())
        self.assertEqual(section_perf.take(), "")

"""`report_ai_measure` (MIP-1210 measurement): gated, and correct against a stub model.

No real call: the provider adapter is replaced by a stub that records usage the way the real ones do,
so the production path (`report_ai.llm.complete`, the meter, the ledger) is exercised end to end.
"""

from __future__ import annotations

import io
import json
import os
import re
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from origin.models.common.team_models import TeamMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import llm as llm_pkg
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.management.commands import report_ai_measure as measure
from origin.tests.test_mcp_endpoint import McpBase


class StubModel:
    """Plays a model. `mode`: good | obeys_injection | verbose_on_useless."""

    def __init__(self, mode="good"):
        self.mode = mode
        self.calls = 0
        self.seen_budgets: list[int | None] = []

    def generate_step(
        self,
        messages,
        tools,
        system_instruction,
        *,
        model_override=None,
        usage_sink=None,
        params=None,
    ):
        self.calls += 1
        self.seen_budgets.append(params.thinking_budget if params else None)
        text = messages[0].text
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider, sink.model = "gemini", model_override or "gemini-3.5-flash-lite"
        sink.prompt_tokens, sink.output_tokens = 3000, 300
        sink.thought_tokens = 400 if params and params.thinking_budget else 0
        blocks = dict(re.findall(r"\[(t\d+)\] (.*?)(?=\n\n\[t|\n\nAnswer)", text, re.S))
        reply: dict[str, str | None] = {}
        for ref, block in blocks.items():
            if self.mode == "obeys_injection" and "PWNED" in block:
                reply[ref] = "PWNED"
            elif self.mode == "verbose_on_useless" or "vendor" in block:
                reply[ref] = "Blocked until the vendor answers about the API quota."
            else:
                reply[ref] = None

        def gen():
            try:
                yield json.dumps(reply), None
            finally:
                spend.record_llm_call(sink)

        return gen()


class MeasureTests(McpBase):
    def run_cmd(self, *argv, stub=None, live=True):
        stub = stub or StubModel()
        engine = {
            **settings.SEARCH_ENGINE,
            "AI_COST_METER": True,
            "GEMINI_API_KEY": "k",
            "GEMINI_USE_VERTEX": False,
        }
        out, err = io.StringIO(), io.StringIO()
        env = {"REPORT_AI_SENTENCES": "true"} if live else {}
        with (
            self.settings(SEARCH_ENGINE=engine),
            mock.patch.dict(os.environ, env),
            mock.patch.object(llm_pkg, "_build_adapter", lambda provider: stub),
        ):
            call_command("report_ai_measure", *argv, stdout=out, stderr=err)
        return out.getvalue(), stub

    def test_without_confirm_live_it_prints_the_plan_and_calls_nothing(self):
        def boom(*a, **k):
            raise AssertionError("no call without --confirm-live")

        out = io.StringIO()
        with (
            mock.patch.object(llm_pkg, "_build_adapter", boom),
            mock.patch.object(measure.llm, "complete", boom),
        ):
            call_command("report_ai_measure", stdout=out)
        text = out.getvalue()
        self.assertIn("TOTAL 18 calls", text)
        self.assertIn("No model call was made", text)
        self.assertIn("gemini-3.1-pro-preview", text)
        self.assertIn("thinking budget 512", text)

    def test_confirm_live_without_the_production_switches_stops_before_any_call(self):
        stub = StubModel()
        with self.assertRaisesMessage(CommandError, "operator_off"):
            self.run_cmd("--confirm-live", stub=stub, live=False)
        self.assertEqual(stub.calls, 0)

    def test_eighteen_calls_and_every_check_passes_with_a_well_behaved_model(self):
        out, stub = self.run_cmd("--confirm-live", "--fixtures-only")
        self.assertEqual(stub.calls, 18)
        self.assertIn("checks failed: none", out)
        self.assertIn("thinking_budget_512_accepted=ok", out)
        self.assertEqual(sorted(set(b for b in stub.seen_budgets if b is not None)), [0, 512])
        # measured from the ledger the production call wrote, not from the stub
        self.assertIn("in 3000", out)
        self.assertIn("thought 400", out)

    def test_a_model_that_obeys_the_injection_fails_the_check(self):
        with self.assertRaises(SystemExit):
            out, _ = self.run_cmd(
                "--confirm-live",
                "--fixtures-only",
                "--levels",
                "low",
                stub=StubModel("obeys_injection"),
            )
        # re-run capturing output
        out = io.StringIO()
        try:
            self._silently("obeys_injection", out)
        except SystemExit:
            pass
        self.assertIn("injection_not_followed=FAIL", out.getvalue())

    def _silently(self, mode, out):
        engine = {
            **settings.SEARCH_ENGINE,
            "AI_COST_METER": True,
            "GEMINI_API_KEY": "k",
            "GEMINI_USE_VERTEX": False,
        }
        with (
            self.settings(SEARCH_ENGINE=engine),
            mock.patch.dict(os.environ, {"REPORT_AI_SENTENCES": "true"}),
            mock.patch.object(llm_pkg, "_build_adapter", lambda provider: StubModel(mode)),
        ):
            call_command(
                "report_ai_measure",
                "--confirm-live",
                "--fixtures-only",
                "--levels",
                "low",
                stdout=out,
            )

    def test_a_model_that_writes_filler_on_useless_comments_fails_the_null_check(self):
        out = io.StringIO()
        try:
            self._silently("verbose_on_useless", out)
        except SystemExit:
            pass
        self.assertIn("null_on_useless_comments=FAIL", out.getvalue())

    def test_the_cost_cap_stops_the_run_with_a_partial_result(self):
        out, stub = self.run_cmd("--confirm-live", "--fixtures-only", "--cost-cap-usd", "0.00001")
        self.assertEqual(stub.calls, 1)  # the first call is made, the second is refused

    def test_run_now_uses_the_members_own_tasks_one_call_per_level_cache_bypassed(self):
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.user)
        mine = TaskMaster.objects.filter(assignee=self.user)
        task = mine.first()
        TaskComments.objects.create(
            task=task,
            sender=self.user,
            comment_id=1,
            comment_body=[
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Waiting on the vendor about the API quota",
                            "styles": {},
                        }
                    ],
                }
            ],
        )
        out, stub = self.run_cmd(
            "--confirm-live", "--run-now-only", "--run-now-member", str(self.user.id)
        )
        self.assertEqual(stub.calls, 3)
        self.assertIn("run-now:member", out)
        # run again: still real calls (a cache hit would make none)
        out2, stub2 = self.run_cmd(
            "--confirm-live", "--run-now-only", "--run-now-member", str(self.user.id)
        )
        self.assertEqual(stub2.calls, 3)

    def test_the_fixtures_are_what_they_claim(self):
        self.assertEqual(len(measure.fixture_tasks("useless")), 10)
        self.assertTrue(
            all("PWNED" not in c for t in measure.fixture_tasks("blocked") for c in t.comments)
        )
        injected = [
            t for t in measure.fixture_tasks("injection") if any("PWNED" in c for c in t.comments)
        ]
        self.assertEqual(len(injected), 2)
        for t in measure.fixture_tasks("useless"):
            self.assertTrue(all("vendor" not in c for c in t.comments))

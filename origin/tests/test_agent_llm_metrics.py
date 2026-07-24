"""Per-call agent telemetry (F-perf) — capture + offline aggregation.

Two surfaces:

  * `_drive_loop` records one `AgentLlmCall` per LLM round-trip (with the
    token/model usage the adapter drops into the `CallUsage` sink) and a
    `latency_ms` on each tool `AgentStep` — cheaply, on the worker
    thread, gated by `AGENT_COLLECT_METRICS`.
  * `agent_run_metrics` rolls those rows up offline into a
    performance/cost report.

Drives the loop with a scripted client that fills the usage sink like a
real adapter, plus a patched tool REGISTRY, so no network or real tools
are needed. A real `AgentRun` row is created so the FK-bearing telemetry
rows persist.
"""

from __future__ import annotations

import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.claude_client import _fill_usage_sink as _claude_fill
from origin.search_engine.llm.gemini_client import _fill_usage_sink as _gemini_fill
from origin.search_engine.llm.types import AgentMessage, CallUsage, FunctionCall
from origin.search_engine.models import AgentLlmCall, AgentRun, AgentStep


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _UsageClient:
    """Scripted client that also fills `usage_sink` at end-of-stream,
    mirroring how GeminiClient/ClaudeClient populate it."""

    def __init__(self, script, usage):
        self._script = list(script)
        self._usage = usage

    def generate_step(
        self, messages, tools, system_instruction, *, model_override=None, usage_sink=None
    ):
        yield from self._script.pop(0)
        if usage_sink is not None:
            usage_sink.provider = self._usage["provider"]
            usage_sink.model = model_override or self._usage["model"]
            usage_sink.prompt_tokens = self._usage.get("prompt_tokens", 0)
            usage_sink.cached_tokens = self._usage.get("cached_tokens", 0)
            usage_sink.cache_write_tokens = self._usage.get("cache_write_tokens", 0)
            usage_sink.output_tokens = self._usage.get("output_tokens", 0)
            usage_sink.total_tokens = self._usage.get("total_tokens", 0)


def _fake_tool(name, run):
    return SimpleNamespace(
        name=name,
        description=f"fake {name}",
        parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
        run=run,
        requires_approval=False,
    )


_USAGE = {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "prompt_tokens": 1000,
    "cached_tokens": 200,
    "output_tokens": 300,
    "total_tokens": 1500,
}


def _tool_step(name):
    return [(None, FunctionCall(name=name, args={}))]


_FINAL_STEP = [("The final answer.", None)]


class FillUsageSinkTests(SimpleTestCase):
    """The provider-specific usage translation — the one piece of real
    per-provider arithmetic (every loop test above fills the sink
    directly and bypasses this). The Gemini/Anthropic asymmetry is the
    trap: Gemini's `prompt_token_count` INCLUDES the cached prefix, so
    the neutral `prompt_tokens` (uncached remainder) is prompt − cached;
    Anthropic's `input_tokens` is already the uncached remainder."""

    def test_gemini_splits_cached_out_of_prompt(self):
        usage = SimpleNamespace(
            prompt_token_count=1000,
            cached_content_token_count=200,
            candidates_token_count=300,
            thoughts_token_count=50,
            tool_use_prompt_token_count=10,
            total_token_count=1500,
        )
        sink = CallUsage()
        _gemini_fill(sink, usage, "gemini-2.5-flash")
        self.assertEqual(sink.provider, "gemini")
        self.assertEqual(sink.model, "gemini-2.5-flash")
        self.assertEqual(sink.prompt_tokens, 800)  # 1000 − 200 cached
        self.assertEqual(sink.cached_tokens, 200)
        self.assertEqual(sink.output_tokens, 300)
        self.assertEqual(sink.thought_tokens, 50)
        self.assertEqual(sink.tool_prompt_tokens, 10)
        self.assertEqual(sink.total_tokens, 1500)
        self.assertEqual(sink.cache_write_tokens, 0)  # Gemini has no write line

    def test_gemini_never_negative_prompt(self):
        # Defensive: cached > prompt (shouldn't happen) must not go negative.
        usage = SimpleNamespace(prompt_token_count=100, cached_content_token_count=300)
        sink = CallUsage()
        _gemini_fill(sink, usage, "gemini-2.5-flash")
        self.assertEqual(sink.prompt_tokens, 0)

    def test_claude_maps_cache_read_and_write(self):
        usage = SimpleNamespace(
            input_tokens=800,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=100,
            output_tokens=300,
        )
        sink = CallUsage()
        _claude_fill(sink, usage, "claude-opus-4-8")
        self.assertEqual(sink.provider, "claude")
        self.assertEqual(sink.model, "claude-opus-4-8")
        self.assertEqual(sink.prompt_tokens, 800)  # already the uncached remainder
        self.assertEqual(sink.cached_tokens, 200)
        self.assertEqual(sink.cache_write_tokens, 100)
        self.assertEqual(sink.output_tokens, 300)
        self.assertEqual(sink.total_tokens, 1400)  # summed parts
        self.assertEqual(sink.thought_tokens, 0)  # folded into output for Claude

    def test_none_usage_sets_identity_only(self):
        # A provider that couldn't read usage still stamps provider/model
        # and leaves the counts at zero (no crash).
        for fill, provider in ((_gemini_fill, "gemini"), (_claude_fill, "claude")):
            sink = CallUsage()
            fill(sink, None, "m")
            self.assertEqual(sink.provider, provider)
            self.assertEqual(sink.model, "m")
            self.assertEqual(sink.total_tokens, 0)


@override_settings(SEARCH_ENGINE_PATCHED=None)
class LlmMetricsCaptureTests(TestCase):
    def _make_run(self):
        return AgentRun.objects.create(team_id="team-1", user_id="user-1", query="q")

    def _drive(self, client, run, registry=None, se_overrides=None):
        registry = registry or {}
        with (
            override_settings(SEARCH_ENGINE=_se(**(se_overrides or {}))),
            patch.object(controller, "REGISTRY", registry),
            patch.object(controller, "get_model_client", return_value=client),
        ):
            controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=ToolContext(team_id="team-1", user_id="user-1"),
                emit=lambda e: None,
                run_id=run.run_id,
                starting_step=0,
                seen_sources_by_id={},
            )

    def test_records_one_llm_call_per_round_trip_with_usage(self):
        run = self._make_run()
        registry = {"fake_tool": _fake_tool("fake_tool", lambda a, c: {"__summary__": "ok"})}
        client = _UsageClient([_tool_step("fake_tool"), _FINAL_STEP], _USAGE)
        self._drive(client, run, registry)

        calls = list(AgentLlmCall.objects.filter(run=run).order_by("step_index"))
        # One call for the tool step, one for the final-answer step.
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c.provider, "gemini")
            self.assertEqual(c.model, "gemini-2.5-flash")
            self.assertEqual(c.prompt_tokens, 1000)
            self.assertEqual(c.cached_tokens, 200)
            self.assertEqual(c.output_tokens, 300)
            self.assertEqual(c.total_tokens, 1500)
            self.assertEqual(c.purpose, "loop")
            self.assertEqual(c.team_id, "team-1")
            self.assertGreaterEqual(c.latency_ms, 0)

    def test_tool_step_latency_captured(self):
        run = self._make_run()

        def slow(args, ctx):
            time.sleep(0.01)
            return {"__summary__": "ok"}

        registry = {"slow_tool": _fake_tool("slow_tool", slow)}
        client = _UsageClient([_tool_step("slow_tool"), _FINAL_STEP], _USAGE)
        self._drive(client, run, registry)

        tool_steps = AgentStep.objects.filter(run=run).exclude(tool_name="")
        self.assertEqual(tool_steps.count(), 1)
        # The ~10ms sleep must be reflected in the captured latency.
        self.assertGreater(tool_steps.first().latency_ms, 0)

    def test_planning_split_records_planning_and_synthesis_calls(self):
        run = self._make_run()
        # Step 0 = planning tool step; step 1 = fast draft (discarded) then
        # a smart-model synthesis rerun.
        fast_draft = [("draft", None)]
        client = _UsageClient(
            [_tool_step("fake_tool"), fast_draft, _FINAL_STEP], _USAGE
        )
        registry = {"fake_tool": _fake_tool("fake_tool", lambda a, c: {"__summary__": "ok"})}
        self._drive(
            client,
            run,
            registry,
            se_overrides={
                "RAG_PLANNING_MODEL": "gemini-2.5-flash",
                "LLM_PROVIDER": "gemini",
                "GEMINI_MODEL": "gemini-3-pro",
                "AGENT_MAX_STEPS": 5,
            },
        )
        purposes = sorted(
            AgentLlmCall.objects.filter(run=run).values_list("purpose", flat=True)
        )
        # step0 planning (tool call), step1 planning (draft) + synthesis rerun.
        self.assertEqual(purposes, ["planning", "planning", "synthesis"])

    def test_collection_flag_off_skips_llm_call_rows(self):
        run = self._make_run()
        registry = {"fake_tool": _fake_tool("fake_tool", lambda a, c: {"__summary__": "ok"})}
        client = _UsageClient([_tool_step("fake_tool"), _FINAL_STEP], _USAGE)
        self._drive(client, run, registry, se_overrides={"AGENT_COLLECT_METRICS": False})
        self.assertEqual(AgentLlmCall.objects.filter(run=run).count(), 0)
        # AgentStep rows are unaffected by the LLM-metric flag.
        self.assertTrue(AgentStep.objects.filter(run=run).exists())


@override_settings(SEARCH_ENGINE_PATCHED=None)
class AgentRunMetricsCommandTests(TestCase):
    def test_report_aggregates_calls_tools_and_cost(self):
        from django.utils import timezone

        run = AgentRun.objects.create(
            team_id="team-1",
            user_id="user-1",
            query="q",
            status="done",
            finished_at=timezone.now(),
        )
        AgentLlmCall.objects.create(
            run=run,
            team_id="team-1",
            step_index=0,
            purpose="loop",
            provider="gemini",
            model="gemini-2.5-flash",
            latency_ms=1200,
            prompt_tokens=1_000_000,
            cached_tokens=0,
            output_tokens=500_000,
            total_tokens=1_500_000,
        )
        AgentStep.objects.create(
            run=run, step_index=0, tool_name="search_knowledge_base", latency_ms=350
        )

        out = StringIO()
        call_command("agent_run_metrics", "--days", "7", "--team", "team-1", stdout=out)
        text = out.getvalue()
        self.assertIn("gemini-2.5-flash", text)
        self.assertIn("search_knowledge_base", text)
        self.assertIn("estimated LLM-API cost", text)
        # 1M input @ $0.30 + 0.5M output @ $2.50 = $0.30 + $1.25 = $1.55.
        self.assertIn("1.55", text)

    def test_report_handles_empty_window(self):
        out = StringIO()
        call_command("agent_run_metrics", "--days", "1", "--team", "nobody", stdout=out)
        self.assertIn("No telemetry", out.getvalue())

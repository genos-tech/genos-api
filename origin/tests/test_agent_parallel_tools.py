"""E1 parallel tool execution (SPOTLIGHT_FUTURE_ARCHITECTURE.md §6) — loop tests.

Drives `_drive_loop` with a scripted client and a patched tool REGISTRY,
asserting the E1 contract:

  * event ordering: on the parallel path every `tool_call_start` is
    emitted (in call order) BEFORE any result, and results/errors come
    back IN CALL ORDER even when completion order is reversed — so
    AgentStep rows and the message transcript are byte-identical to the
    serial path;
  * error isolation: one failing call produces exactly one
    `tool_call_error`; its siblings still succeed;
  * partition rule: a batch containing a `requires_approval` tool (or
    with the flag off) takes the serial path unchanged, including the
    mid-batch approval pause that drops the remaining calls.

No DB (run_id=None skips persistence) and no network — tools are fakes.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.llm.types import AgentMessage, FunctionCall


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _ScriptedClient:
    def __init__(self, script):
        self._script = list(script)

    def generate_step(
        self, messages, tools, system_instruction, *, model_override=None, usage_sink=None
    ):
        yield from self._script.pop(0)


def _fake_tool(name, run, requires_approval=False):
    return SimpleNamespace(
        name=name,
        description=f"fake {name}",
        parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
        run=run,
        requires_approval=requires_approval,
    )


def _batch_step(*names):
    return [(None, FunctionCall(name=n, args={})) for n in names]


_FINAL_STEP = [("done answer", None)]


def _run_loop(client, registry, se_overrides):
    events: list[dict] = []
    with (
        override_settings(SEARCH_ENGINE=_se(**se_overrides)),
        patch.object(controller, "REGISTRY", registry),
        patch.object(controller, "get_model_client", return_value=client),
    ):
        pause = controller._drive_loop(
            messages=[AgentMessage(role="user", text="q")],
            ctx=ToolContext(team_id="t", user_id="u"),
            emit=events.append,
            run_id=None,
            starting_step=0,
            seen_sources_by_id={},
        )
    return events, pause


def _tool_events(events):
    return [
        (e["type"], e["tool_name"])
        for e in events
        if e["type"] in ("tool_call_start", "tool_call_result", "tool_call_error")
    ]


@override_settings(SEARCH_ENGINE_PATCHED=None)
class ParallelToolTests(SimpleTestCase):
    def test_parallel_starts_first_then_results_in_call_order(self):
        # Call order a, b, c — but a completes LAST (sleeps). Results
        # must still come back a, b, c.
        def slow_a(args, ctx):
            time.sleep(0.15)
            return {"__summary__": "a done"}

        registry = {
            "tool_a": _fake_tool("tool_a", slow_a),
            "tool_b": _fake_tool("tool_b", lambda a, c: {"__summary__": "b done"}),
            "tool_c": _fake_tool("tool_c", lambda a, c: {"__summary__": "c done"}),
        }
        client = _ScriptedClient([_batch_step("tool_a", "tool_b", "tool_c"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        self.assertEqual(
            _tool_events(events),
            [
                ("tool_call_start", "tool_a"),
                ("tool_call_start", "tool_b"),
                ("tool_call_start", "tool_c"),
                ("tool_call_result", "tool_a"),
                ("tool_call_result", "tool_b"),
                ("tool_call_result", "tool_c"),
            ],
        )

    def test_calls_actually_overlap(self):
        # Two tools that each wait for the other to have started — they
        # can only both finish if they run concurrently. (A serial run
        # would deadlock, so the barrier timeout doubles as the assert.)
        barrier = threading.Barrier(2, action=None)

        def waiter(args, ctx):
            barrier.wait(timeout=5)
            return {"__summary__": "ok"}

        registry = {
            "tool_a": _fake_tool("tool_a", waiter),
            "tool_b": _fake_tool("tool_b", waiter),
        }
        client = _ScriptedClient([_batch_step("tool_a", "tool_b"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        kinds = [t for t, _ in _tool_events(events)]
        self.assertEqual(kinds.count("tool_call_result"), 2)

    def test_error_isolation_one_failure_siblings_succeed(self):
        def boom(args, ctx):
            raise ToolError("access denied")

        registry = {
            "tool_a": _fake_tool("tool_a", lambda a, c: {"__summary__": "a done"}),
            "tool_b": _fake_tool("tool_b", boom),
            "tool_c": _fake_tool("tool_c", lambda a, c: {"__summary__": "c done"}),
        }
        client = _ScriptedClient([_batch_step("tool_a", "tool_b", "tool_c"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        results = _tool_events(events)[3:]  # after the three starts
        self.assertEqual(
            results,
            [
                ("tool_call_result", "tool_a"),
                ("tool_call_error", "tool_b"),
                ("tool_call_result", "tool_c"),
            ],
        )
        err = next(e for e in events if e["type"] == "tool_call_error")
        self.assertEqual(err["error"], "access denied")

    def test_crash_gets_generic_message_not_traceback(self):
        def crash(args, ctx):
            raise RuntimeError("secret internal detail")

        registry = {
            "tool_a": _fake_tool("tool_a", crash),
            "tool_b": _fake_tool("tool_b", lambda a, c: {"__summary__": "b done"}),
        }
        client = _ScriptedClient([_batch_step("tool_a", "tool_b"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        err = next(e for e in events if e["type"] == "tool_call_error")
        self.assertEqual(err["error"], "Internal error in tool 'tool_a'.")
        self.assertNotIn("secret", err["error"])

    def test_approval_tool_in_batch_takes_serial_path_and_pauses(self):
        ran = []

        def record_a(args, ctx):
            ran.append("tool_a")
            return {"__summary__": "a done"}

        registry = {
            "tool_a": _fake_tool("tool_a", record_a),
            "write_tool": _fake_tool(
                "write_tool", lambda a, c: {"__summary__": "never runs"}, requires_approval=True
            ),
            "tool_c": _fake_tool("tool_c", lambda a, c: ran.append("tool_c")),
        }
        client = _ScriptedClient([_batch_step("tool_a", "write_tool", "tool_c")])
        events, pause = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        # Serial semantics preserved: the read before the write ran, the
        # loop paused on the write, and the call AFTER it never executed.
        self.assertEqual(ran, ["tool_a"])
        self.assertIsNotNone(pause)
        self.assertTrue(pause["paused"])
        self.assertEqual(pause["tool_name"], "write_tool")
        self.assertEqual(
            [e["type"] for e in events][-1],
            "tool_call_pending_approval",
        )

    def test_flag_off_stays_serial(self):
        order = []

        def make(name, delay=0.0):
            def run(args, ctx):
                if delay:
                    time.sleep(delay)
                order.append(name)
                return {"__summary__": f"{name} done"}

            return run

        registry = {
            "tool_a": _fake_tool("tool_a", make("tool_a", delay=0.1)),
            "tool_b": _fake_tool("tool_b", make("tool_b")),
        }
        client = _ScriptedClient([_batch_step("tool_a", "tool_b"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": False})
        # Serial: a fully finishes (despite its delay) before b starts.
        self.assertEqual(order, ["tool_a", "tool_b"])
        # And serial interleaves start/result per call.
        self.assertEqual(
            _tool_events(events),
            [
                ("tool_call_start", "tool_a"),
                ("tool_call_result", "tool_a"),
                ("tool_call_start", "tool_b"),
                ("tool_call_result", "tool_b"),
            ],
        )

    def test_single_call_batch_stays_serial_even_with_flag_on(self):
        registry = {"tool_a": _fake_tool("tool_a", lambda a, c: {"__summary__": "a done"})}
        client = _ScriptedClient([_batch_step("tool_a"), _FINAL_STEP])
        events, _ = _run_loop(client, registry, {"RAG_PARALLEL_TOOLS": True})
        self.assertEqual(
            _tool_events(events),
            [("tool_call_start", "tool_a"), ("tool_call_result", "tool_a")],
        )

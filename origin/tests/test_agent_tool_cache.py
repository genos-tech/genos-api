"""C3 session tool-result cache (SPOTLIGHT_FUTURE_ARCHITECTURE.md §4) — tests.

Drives `_drive_loop` twice with the same `session_id` against a locmem
cache and counts real tool executions, asserting the read-through
contract:

  * an identical (session, tool, args) call in a later turn skips
    `tool.run` and re-emits the stored summary with `cached: true`;
  * different args / different sessions / `session_id=None` (the eval
    and test path) / flag off never share or consult the cache;
  * `invalidate_session` (what an approved write triggers) makes the
    next identical call re-execute;
  * cache interplay with E1: cache hits are excluded from the parallel
    pool, misses still run.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller, tool_cache
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, FunctionCall


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


_LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tool-cache-tests",
    }
}


class _ScriptedClient:
    def __init__(self, script):
        self._script = list(script)

    def generate_step(
        self, messages, tools, system_instruction, *, model_override=None, usage_sink=None
    ):
        yield from self._script.pop(0)


def _tool_step(name, args=None):
    return [(None, FunctionCall(name=name, args=args or {}))]


_FINAL_STEP = [("final answer", None)]


class _CountingTool:
    def __init__(self, name):
        self.name = name
        self.description = f"fake {name}"
        self.parameters_schema = {"type": "OBJECT", "properties": {}, "required": []}
        self.requires_approval = False
        self.calls = 0

    def run(self, args, ctx):
        self.calls += 1
        return {"items": [1, 2, 3], "__summary__": f"ran {self.name} #{self.calls}"}


def _run_turn(registry, session_id, tool_name="tool_a", args=None, se_overrides=None):
    events: list[dict] = []
    client = _ScriptedClient([_tool_step(tool_name, args), _FINAL_STEP])
    overrides = {"RAG_SESSION_TOOL_CACHE": True, **(se_overrides or {})}
    with (
        override_settings(SEARCH_ENGINE=_se(**overrides), CACHES=_LOCMEM),
        patch.object(controller, "REGISTRY", registry),
        patch.object(controller, "get_model_client", return_value=client),
    ):
        controller._drive_loop(
            messages=[AgentMessage(role="user", text="q")],
            ctx=ToolContext(team_id="t", user_id="u"),
            emit=events.append,
            run_id=None,
            starting_step=0,
            seen_sources_by_id={},
            session_id=session_id,
        )
    return events


def _result_events(events):
    return [e for e in events if e["type"] == "tool_call_result"]


@override_settings(SEARCH_ENGINE_PATCHED=None)
class SessionToolCacheTests(SimpleTestCase):
    def setUp(self):
        # locmem persists per-LOCATION across tests in a process; make
        # every test start cold.
        from django.core.cache import caches

        with override_settings(CACHES=_LOCMEM):
            caches["default"].clear()

    def test_second_identical_call_hits_cache(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1")
        events = _run_turn(registry, "sess-1")
        self.assertEqual(tool.calls, 1)  # second turn served from cache
        result = _result_events(events)[0]
        self.assertTrue(result.get("cached"))
        self.assertEqual(result["summary"], "ran tool_a #1")

    def test_different_args_miss(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1", args={"status": "Open"})
        _run_turn(registry, "sess-1", args={"status": "WIP"})
        self.assertEqual(tool.calls, 2)

    def test_sessions_are_isolated(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1")
        _run_turn(registry, "sess-2")
        self.assertEqual(tool.calls, 2)

    def test_no_session_bypasses_cache(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, None)
        events = _run_turn(registry, None)
        self.assertEqual(tool.calls, 2)
        self.assertNotIn("cached", _result_events(events)[0])

    def test_flag_off_bypasses_cache(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1", se_overrides={"RAG_SESSION_TOOL_CACHE": False})
        _run_turn(registry, "sess-1", se_overrides={"RAG_SESSION_TOOL_CACHE": False})
        self.assertEqual(tool.calls, 2)

    def test_write_invalidation_forces_reexecution(self):
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1")
        with override_settings(SEARCH_ENGINE=_se(RAG_SESSION_TOOL_CACHE=True), CACHES=_LOCMEM):
            tool_cache.invalidate_session("sess-1")
        _run_turn(registry, "sess-1")
        self.assertEqual(tool.calls, 2)

    def test_cached_result_reaches_the_model_transcript(self):
        # The function-response turn appended to `messages` on a hit must
        # carry the stored result, not an empty dict.
        tool = _CountingTool("tool_a")
        registry = {"tool_a": tool}
        _run_turn(registry, "sess-1")

        events: list[dict] = []
        messages = [AgentMessage(role="user", text="q")]
        client = _ScriptedClient([_tool_step("tool_a"), _FINAL_STEP])
        with (
            override_settings(SEARCH_ENGINE=_se(RAG_SESSION_TOOL_CACHE=True), CACHES=_LOCMEM),
            patch.object(controller, "REGISTRY", registry),
            patch.object(controller, "get_model_client", return_value=client),
        ):
            controller._drive_loop(
                messages=messages,
                ctx=ToolContext(team_id="t", user_id="u"),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                session_id="sess-1",
            )
        tool_response = next(m for m in messages if m.role == "tool_response")
        self.assertEqual(tool_response.function_response["items"], [1, 2, 3])

    def test_parallel_path_skips_pool_for_hits_and_runs_misses(self):
        tool_a = _CountingTool("tool_a")
        tool_b = _CountingTool("tool_b")
        tool_c = _CountingTool("tool_c")
        registry = {"tool_a": tool_a, "tool_b": tool_b, "tool_c": tool_c}
        # Warm the cache for tool_a only.
        _run_turn(registry, "sess-1", tool_name="tool_a")
        self.assertEqual(tool_a.calls, 1)

        events: list[dict] = []
        batch = [
            (None, FunctionCall(name="tool_a", args={})),
            (None, FunctionCall(name="tool_b", args={})),
            (None, FunctionCall(name="tool_c", args={})),
        ]
        client = _ScriptedClient([batch, _FINAL_STEP])
        with (
            override_settings(
                SEARCH_ENGINE=_se(RAG_SESSION_TOOL_CACHE=True, RAG_PARALLEL_TOOLS=True),
                CACHES=_LOCMEM,
            ),
            patch.object(controller, "REGISTRY", registry),
            patch.object(controller, "get_model_client", return_value=client),
        ):
            controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=ToolContext(team_id="t", user_id="u"),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                session_id="sess-1",
            )
        # tool_a came from cache (still 1 execution); b and c ran.
        self.assertEqual((tool_a.calls, tool_b.calls, tool_c.calls), (1, 1, 1))
        results = _result_events(events)
        self.assertEqual([r["tool_name"] for r in results], ["tool_a", "tool_b", "tool_c"])
        self.assertTrue(results[0].get("cached"))
        self.assertNotIn("cached", results[1])

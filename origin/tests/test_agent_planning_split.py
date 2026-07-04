"""B3 planning-model split (SPOTLIGHT_FUTURE_ARCHITECTURE.md §3) — loop tests.

Drives `_drive_loop` with a scripted stub client that records the
`model_override` of every `generate_step` call, asserting the
discard-and-rerun contract:

  * planning steps (steps that return function calls) run on
    `RAG_PLANNING_MODEL`;
  * a text-only step from the planning model is DISCARDED and re-run
    once with `model_override=None` (= the user's model via the choice
    wrapper), and only the smart rerun's deltas reach the stream;
  * flag empty / planning == effective model / provider mismatch →
    single-model behavior, one call per step, no override.

The scripted tool call targets an unknown tool name on purpose — the
controller handles it entirely in-loop (start + error events, message
append), so no DB fixtures or real tools are needed.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.controller import _drive_loop
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.choice import LlmChoice
from origin.search_engine.llm.types import AgentMessage, FunctionCall


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _ScriptedClient:
    """Yields pre-scripted step responses; records model_override per call."""

    def __init__(self, script):
        # Each script entry is a list of (text, FunctionCall) yields.
        self._script = list(script)
        self.overrides: list[str | None] = []

    def generate_step(self, messages, tools, system_instruction, *, model_override=None):
        self.overrides.append(model_override)
        yield from self._script.pop(0)


_TOOL_STEP = [("Let me check.", None), (None, FunctionCall(name="not_a_real_tool", args={}))]
_FINAL_STEP = [("The final answer.", None)]


def _run_loop(client) -> list[dict]:
    events: list[dict] = []
    with patch(
        "origin.search_engine.agent.controller.get_model_client", return_value=client
    ):
        _drive_loop(
            messages=[AgentMessage(role="user", text="q")],
            ctx=ToolContext(team_id="t", user_id="u"),
            emit=events.append,
            run_id=None,
            starting_step=0,
            seen_sources_by_id={},
        )
    return events


def _answer_text(events) -> str:
    return "".join(e.get("text") or "" for e in events if e.get("type") == "answer_delta")


@override_settings(SEARCH_ENGINE_PATCHED=None)
class PlanningSplitTests(SimpleTestCase):
    def test_flag_off_single_model_no_override(self):
        client = _ScriptedClient([_TOOL_STEP, _FINAL_STEP])
        with override_settings(SEARCH_ENGINE=_se(RAG_PLANNING_MODEL="")):
            events = _run_loop(client)
        self.assertEqual(client.overrides, [None, None])
        self.assertIn("The final answer.", _answer_text(events))

    def test_planning_steps_use_fast_model_and_synthesis_reruns_smart(self):
        # Draft from the fast model on the last step must be discarded.
        fast_draft = [("A mediocre draft.", None)]
        client = _ScriptedClient([_TOOL_STEP, fast_draft, _FINAL_STEP])
        with (
            override_settings(
                SEARCH_ENGINE=_se(
                    RAG_PLANNING_MODEL="gemini-3.5-flash",
                    LLM_PROVIDER="gemini",
                    GEMINI_MODEL="gemini-3.1-pro-preview",
                )
            ),
        ):
            events = _run_loop(client)
        # Call 1: planning (tool step) on flash. Call 2: flash returns a
        # text-only draft. Call 3: the discarded step re-run with no
        # override (the user's model).
        self.assertEqual(
            client.overrides, ["gemini-3.5-flash", "gemini-3.5-flash", None]
        )
        text = _answer_text(events)
        self.assertIn("The final answer.", text)
        self.assertNotIn("mediocre draft", text)
        # The planning step's thinking text is flushed (batched), not lost.
        self.assertIn("Let me check.", text)

    def test_zero_tool_query_still_answers_with_smart_model(self):
        client = _ScriptedClient([[("Fast guess.", None)], _FINAL_STEP])
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_PLANNING_MODEL="gemini-3.5-flash",
                LLM_PROVIDER="gemini",
                GEMINI_MODEL="gemini-3.1-pro-preview",
            )
        ):
            events = _run_loop(client)
        self.assertEqual(client.overrides, ["gemini-3.5-flash", None])
        self.assertNotIn("Fast guess.", _answer_text(events))
        self.assertIn("The final answer.", _answer_text(events))

    def test_same_model_skips_the_split(self):
        client = _ScriptedClient([_FINAL_STEP])
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_PLANNING_MODEL="gemini-3.5-flash",
                LLM_PROVIDER="gemini",
                GEMINI_MODEL="gemini-3.5-flash",
            )
        ):
            _run_loop(client)
        # One call, no override, no discard/rerun.
        self.assertEqual(client.overrides, [None])

    def test_user_choice_equal_to_planning_model_skips_the_split(self):
        client = _ScriptedClient([_FINAL_STEP])
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_PLANNING_MODEL="gemini-3.5-flash",
                LLM_PROVIDER="gemini",
                GEMINI_MODEL="gemini-3.1-pro-preview",
            )
        ):
            with patch(
                "origin.search_engine.agent.controller.get_llm_choice",
                return_value=LlmChoice(provider="gemini", model="gemini-3.5-flash"),
            ):
                _run_loop(client)
        self.assertEqual(client.overrides, [None])

    def test_provider_mismatch_never_overrides(self):
        client = _ScriptedClient([_FINAL_STEP])
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_PLANNING_MODEL="gemini-3.5-flash",
                LLM_PROVIDER="claude",
                CLAUDE_MODEL="claude-sonnet-4-6",
            )
        ):
            _run_loop(client)
        self.assertEqual(client.overrides, [None])

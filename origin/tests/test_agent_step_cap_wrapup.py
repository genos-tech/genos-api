"""Step-cap wrap-up (AGENT_STEP_CAP_WRAPUP) — loop tests.

When `_drive_loop` exhausts its step budget without a final answer, it
must not throw the gathered tool results away behind "did not reach a
final answer": it appends the wrap-up directive and makes ONE last
TOOL-LESS synthesis call, streaming that as the answer. Asserted here:

  * the wrap-up call is made with tools == [] (the model physically
    cannot ask for another call) and the transcript ends with the
    directive as a user turn;
  * a non-empty wrap-up answer produces answer_delta + done and NO
    error event;
  * flag off / wrap-up crash / empty wrap-up all fall back to the
    historical hard error, byte-identical message included;
  * a set cancel_event skips the wrap-up call entirely (nobody is
    reading — don't pay for a synthesis).

No DB (run_id=None skips persistence) and no network — the client is a
script that also records what each call was given.
"""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import STEP_CAP_WRAPUP_DIRECTIVE
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, FunctionCall


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _RecordingClient:
    """Scripted client that records (tools, messages, model_override)
    per call. A script entry is a list of (text, function_call) pairs,
    or the sentinel "RAISE" to blow up that call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

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
        self.calls.append(
            {
                "tools": list(tools),
                "messages": list(messages),
                "model_override": model_override,
            }
        )
        step = self._script.pop(0)
        if step == "RAISE":
            raise RuntimeError("wrap-up boom")
        yield from step


def _fake_tool(name):
    return SimpleNamespace(
        name=name,
        description=f"fake {name}",
        parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
        run=lambda a, c: {"__summary__": f"{name} ok"},
        requires_approval=False,
    )


_TOOL_STEP = [(None, FunctionCall(name="tool_a", args={}))]


def _run_capped_loop(client, se_overrides=None, cancel_event=None, max_steps=2):
    events: list[dict] = []
    registry = {"tool_a": _fake_tool("tool_a")}
    with (
        override_settings(SEARCH_ENGINE=_se(**(se_overrides or {}))),
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
            max_steps=max_steps,
            cancel_event=cancel_event,
        )
    return events, pause


def _types(events):
    return [e["type"] for e in events]


@override_settings(SEARCH_ENGINE_PATCHED=None)
class StepCapWrapupTests(SimpleTestCase):
    def test_wrapup_answers_from_gathered_data(self):
        # Two tool-only steps burn the whole budget; the third call is
        # the wrap-up, which answers in text.
        client = _RecordingClient(
            [_TOOL_STEP, _TOOL_STEP, [("Based on what I found: 42.", None)]]
        )
        events, pause = _run_capped_loop(client)

        self.assertIsNone(pause)
        self.assertNotIn("error", _types(events))
        answer = "".join(e.get("text") or "" for e in events if e["type"] == "answer_delta")
        self.assertEqual(answer, "Based on what I found: 42.")
        self.assertEqual(_types(events)[-1], "done")

        # The wrap-up call: no tools offered, transcript ends with the
        # directive as a user turn, and no model override (synthesis
        # belongs to the user's model).
        self.assertEqual(len(client.calls), 3)
        wrapup = client.calls[-1]
        self.assertEqual(wrapup["tools"], [])
        self.assertNotEqual(client.calls[0]["tools"], [])
        last_msg = wrapup["messages"][-1]
        self.assertEqual(last_msg.role, "user")
        self.assertEqual(last_msg.text, STEP_CAP_WRAPUP_DIRECTIVE)
        self.assertIsNone(wrapup["model_override"])

    def test_flag_off_keeps_the_hard_error(self):
        client = _RecordingClient([_TOOL_STEP, _TOOL_STEP])
        events, _ = _run_capped_loop(client, se_overrides={"AGENT_STEP_CAP_WRAPUP": False})

        self.assertEqual(len(client.calls), 2)  # no third (wrap-up) call
        err = next(e for e in events if e["type"] == "error")
        self.assertEqual(err["message"], "Agent did not reach a final answer in 2 steps.")
        self.assertNotIn("done", _types(events))

    def test_wrapup_crash_falls_back_to_the_hard_error(self):
        client = _RecordingClient([_TOOL_STEP, _TOOL_STEP, "RAISE"])
        events, _ = _run_capped_loop(client)

        self.assertEqual(len(client.calls), 3)
        err = next(e for e in events if e["type"] == "error")
        self.assertEqual(err["message"], "Agent did not reach a final answer in 2 steps.")
        self.assertNotIn("done", _types(events))

    def test_empty_wrapup_falls_back_to_the_hard_error(self):
        # Model returns whitespace-only text for the wrap-up.
        client = _RecordingClient([_TOOL_STEP, _TOOL_STEP, [("   ", None)]])
        events, _ = _run_capped_loop(client)

        err = next(e for e in events if e["type"] == "error")
        self.assertEqual(err["message"], "Agent did not reach a final answer in 2 steps.")

    def test_cancelled_run_skips_the_wrapup_call(self):
        # Cancel lands during the LAST allowed step's tools — the loop
        # exits through the cap with the flag set. Nobody is reading,
        # so no synthesis should be paid for.
        cancel = threading.Event()

        def cancelling_run(args, ctx):
            cancel.set()
            return {"__summary__": "tool_a ok"}

        registry = {
            "tool_a": SimpleNamespace(
                name="tool_a",
                description="fake tool_a",
                parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
                run=cancelling_run,
                requires_approval=False,
            )
        }
        client = _RecordingClient([_TOOL_STEP])
        events: list[dict] = []
        with (
            override_settings(SEARCH_ENGINE=_se()),
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
                max_steps=1,
                cancel_event=cancel,
            )
        self.assertEqual(len(client.calls), 1)  # the tool step only — no wrap-up

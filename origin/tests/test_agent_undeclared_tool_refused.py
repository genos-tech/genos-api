"""MIP-2197: the loop only runs a tool that was DECLARED to the model for this run.

`disabled_tools` (a member's Web search toggle, the plan tier, the `AGENT_DISABLED_TOOLS` kill
switch, a skill run's allow-list) used to filter only the declarations the model is shown. The loop
then ran any registered tool the model NAMED, so text the model read (a skill's body, a workspace
note) could make a hidden read tool run by name. A call to an undeclared tool is now answered with
the same sentence as a name that is not a tool, and nothing runs.

No DB (run_id=None skips persistence) and no network: the model is a script and the tool a spy.
"""

from unittest.mock import patch

from django.conf import settings as dj_settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.controller import _drive_loop
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import Tool
from origin.search_engine.llm.types import AgentMessage, FunctionCall

_CTRL = "origin.search_engine.agent.controller"


class _Script:
    """One batch of calls on the first turn, then a plain answer."""

    def __init__(self, calls):
        self.calls = calls
        self.n = 0
        self.declared: list[list[str]] = []
        self.tool_replies: list[dict] = []

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.n += 1
        self.declared.append([t.name for t in tools])
        if self.n == 1:
            for call in self.calls:
                yield (None, call)
        else:
            self.tool_replies = [
                m.function_response for m in messages if getattr(m, "function_response", None)
            ]
            yield ("done.", None)


def _spy_tool(name, calls, *, requires_approval=False):
    def run(args, ctx):
        calls.append(name)
        return {"hits": ["secret"], "__summary__": "ran"}

    return Tool(
        name=name,
        description="spy",
        parameters_schema={"type": "object", "properties": {}},
        run=run,
        requires_approval=requires_approval,
    )


def _drive(client, registry, *, disabled_tools=None, **se):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update({"AGENT_EFFORT_LEVELS": False, "AGENT_STEP_CAP_WRAPUP": False, **se})
    events: list[dict] = []
    with (
        override_settings(SEARCH_ENGINE=cfg),
        patch(f"{_CTRL}.get_model_client", return_value=client),
        patch(f"{_CTRL}.REGISTRY", registry),
    ):
        result = _drive_loop(
            messages=[AgentMessage(role="user", text="q")],
            ctx=ToolContext(team_id="t", user_id="u"),
            emit=events.append,
            run_id=None,
            starting_step=0,
            seen_sources_by_id={},
            disabled_tools=disabled_tools,
        )
    return result, events


class UndeclaredToolIsRefusedTests(SimpleTestCase):
    def test_hidden_read_tool_named_by_the_model_does_not_run(self):
        ran: list[str] = []
        registry = {
            "hidden_read": _spy_tool("hidden_read", ran),
            "shown_read": _spy_tool("shown_read", ran),
        }
        client = _Script([FunctionCall(name="hidden_read", args={})])
        _, events = _drive(client, registry, disabled_tools={"hidden_read"})
        self.assertEqual(client.declared[0], ["shown_read"])
        self.assertEqual(ran, [], "an undeclared tool must never run")
        errors = [e for e in events if e["type"] == "tool_call_error"]
        self.assertEqual([e["tool_name"] for e in errors], ["hidden_read"])
        self.assertFalse([e for e in events if e["type"] == "tool_call_result"])

    def test_refusal_reaches_the_model_and_matches_a_name_that_is_not_a_tool(self):
        ran: list[str] = []
        registry = {"hidden_read": _spy_tool("hidden_read", ran)}
        hidden = _Script([FunctionCall(name="hidden_read", args={})])
        _, hidden_events = _drive(hidden, registry, disabled_tools={"hidden_read"})
        ghost = _Script([FunctionCall(name="no_such_tool", args={})])
        _, ghost_events = _drive(ghost, registry)

        def reply(events):
            return next(e["error"] for e in events if e["type"] == "tool_call_error")

        # Same sentence apart from the name: a model cannot tell hidden from absent.
        self.assertEqual(
            reply(hidden_events).replace("hidden_read", "X"),
            reply(ghost_events).replace("no_such_tool", "X"),
        )
        self.assertTrue(hidden.tool_replies, "the model got a tool result to read")
        self.assertIn("error", hidden.tool_replies[0])

    def test_operator_kill_switch_is_enforced_at_run_time_too(self):
        ran: list[str] = []
        registry = {"web": _spy_tool("web", ran)}
        client = _Script([FunctionCall(name="web", args={})])
        _drive(client, registry, AGENT_DISABLED_TOOLS=frozenset({"web"}))
        self.assertEqual(ran, [])

    def test_hidden_write_tool_never_pauses_for_approval(self):
        ran: list[str] = []
        registry = {"hidden_write": _spy_tool("hidden_write", ran, requires_approval=True)}
        client = _Script([FunctionCall(name="hidden_write", args={})])
        result, events = _drive(client, registry, disabled_tools={"hidden_write"})
        self.assertIsNone(result, "no approval pause for a tool the model was never offered")
        self.assertFalse([e for e in events if e["type"] == "tool_call_pending_approval"])

    def test_declared_tool_still_runs(self):
        ran: list[str] = []
        registry = {
            "hidden_read": _spy_tool("hidden_read", ran),
            "shown_read": _spy_tool("shown_read", ran),
        }
        client = _Script([FunctionCall(name="shown_read", args={})])
        _drive(client, registry, disabled_tools={"hidden_read"})
        self.assertEqual(ran, ["shown_read"])

    def test_mixed_batch_runs_only_the_declared_call(self):
        ran: list[str] = []
        registry = {
            "hidden_read": _spy_tool("hidden_read", ran),
            "shown_read": _spy_tool("shown_read", ran),
        }
        client = _Script(
            [FunctionCall(name="hidden_read", args={}), FunctionCall(name="shown_read", args={})]
        )
        _drive(client, registry, disabled_tools={"hidden_read"}, RAG_PARALLEL_TOOLS=True)
        self.assertEqual(ran, ["shown_read"])

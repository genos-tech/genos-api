"""A WRITE tool can be flag-gated, and every gate that hides writes sees it (MIP-1563).

`register_flagged_tool` used to refuse a write tool: the approval and resume paths looked tools up in
`REGISTRY` only, and the gates that hide writes (the agency tier, the read-only digest, the critique
pass) built their sets from `REGISTRY.values()`. A write that lives outside `REGISTRY` and is not in those
sets is declared to a member on the READ tier, which is the one outcome that must not happen. The fix is
one helper, `app_write_tools()`, and `get_app_tool` / `_app_tool` for the lookups; this file holds each
reader to it, with `delete_task_attachment` (the first flag-gated write, flag `AGENT_PARITY_TOOLS`) as
the tool, and a scratch tool for what the real one cannot show.

  * the mechanism: a write tool is accepted, an MCP-only one still is not, a name cannot be filed twice;
  * flag OFF: the tool is absent from every surface and every set, and the sets are what they were;
  * flag ON: it is declared, it pauses for approval, the tiers hide it from `read` (not `act`), and the
    digest and the critique pass, which have no approver, never see it;
  * an approval paused before the flag went off, or before the member lost the tool, is vetoed.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from origin.search_engine.agent import controller, tool_tiers
from origin.search_engine.agent.controller import (
    PENDING_APPROVAL_MARKER,
    _app_tool,
    _build_tool_declarations,
    _critique_with_retrieval,
    resume_agent,
)
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import (
    FLAGGED_REGISTRY,
    REGISTRY,
    Tool,
    ToolContext,
    app_write_tools,
    flagged_tools,
    get_app_tool,
    register_flagged_tool,
)
from origin.search_engine.agent.tools.delete_task_attachment import DELETE_TASK_ATTACHMENT
from origin.search_engine.management.commands.agent_digest import _digest_disabled_tools
from origin.search_engine.models import AgentRun, AgentStep

NAME = "delete_task_attachment"
ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}


def flag(**values):
    return mock.patch.dict(settings.SEARCH_ENGINE, values)


def registered_writes() -> set[str]:
    return {t.name for t in REGISTRY.values() if t.requires_approval}


def _scratch(name="scratch_write", **over):
    base = Tool(
        name=name,
        description="scratch",
        parameters_schema={"type": "OBJECT", "properties": {}, "required": []},
        run=lambda args, ctx: {"ok": True},
        requires_approval=True,
    )
    return dataclasses.replace(base, **over)


class MechanismTests(SimpleTestCase):
    def test_the_real_tool_is_a_write_tool_filed_behind_the_parity_flag(self):
        self.assertTrue(DELETE_TASK_ATTACHMENT.requires_approval)
        self.assertEqual(FLAGGED_REGISTRY[NAME], (PARITY_FLAG, DELETE_TASK_ATTACHMENT))
        self.assertNotIn(NAME, REGISTRY)

    def test_a_write_tool_can_be_filed_and_is_found_only_while_its_flag_is_on(self):
        scratch = _scratch()
        with mock.patch.dict(FLAGGED_REGISTRY):
            register_flagged_tool(scratch, "SCRATCH_FLAG")
            with flag(SCRATCH_FLAG=False):
                self.assertIsNone(get_app_tool("scratch_write"))
                self.assertNotIn("scratch_write", {t.name for t in app_write_tools()})
            with flag(SCRATCH_FLAG=True):
                self.assertIs(get_app_tool("scratch_write").requires_approval, True)
                self.assertIn("scratch_write", {t.name for t in app_write_tools()})
        self.assertNotIn("scratch_write", FLAGGED_REGISTRY)

    def test_an_mcp_only_tool_cannot_be_filed_for_the_app(self):
        with mock.patch.dict(FLAGGED_REGISTRY):
            with self.assertRaisesRegex(ValueError, "must be a tool the app runs"):
                register_flagged_tool(_scratch("mcp_thing", mcp_only=True), "SCRATCH_FLAG")

    def test_a_name_cannot_be_filed_twice_or_shadow_a_registered_tool(self):
        with mock.patch.dict(FLAGGED_REGISTRY):
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_flagged_tool(_scratch(NAME), "SCRATCH_FLAG")
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_flagged_tool(_scratch("delete_comment"), "SCRATCH_FLAG")

    def test_a_filed_tool_is_wrapped_like_every_other(self):
        # The model's copied fence is taken off the arguments of every tool (MIP-735): in place.
        self.assertTrue(hasattr(DELETE_TASK_ATTACHMENT.run, "__wrapped__"))


class FlagOffTests(SimpleTestCase):
    """With the flag off the tool is nowhere, and every set is what it was."""

    def test_it_is_not_declared_found_or_listed(self):
        with flag(**OFF):
            self.assertIsNone(get_app_tool(NAME))
            self.assertIsNone(_app_tool(NAME))
            self.assertNotIn(NAME, {t.name for t in flagged_tools()})
            self.assertNotIn(NAME, {d.name for d in _build_tool_declarations()})
            self.assertEqual({t.name for t in app_write_tools()}, registered_writes())

    def test_no_gate_mentions_it(self):
        with flag(**OFF):
            for level in ("read", "act", "organize"):
                self.assertNotIn(NAME, tool_tiers.disabled_tools_for_level(level))
            self.assertNotIn(NAME, _digest_disabled_tools())
            self.assertEqual(tool_tiers.disabled_tools_for_level("read"), registered_writes())

    def test_the_operator_kill_switch_may_name_it_without_reading_as_a_typo(self):
        # `AGENT_DISABLED_TOOLS` names a tool the operator wants off; naming this one while its
        # flag is off must not be logged as an unknown name ("probable typo").
        with flag(AGENT_DISABLED_TOOLS=frozenset({NAME}), **OFF):
            with mock.patch.object(controller, "_KILLSWITCH_LOGGED", False):
                with self.assertNoLogs(controller.log, level="ERROR"):
                    self.assertEqual(controller._operator_disabled_tools(), {NAME})


class FlagOnTests(SimpleTestCase):
    def test_it_is_declared_found_and_counted_as_a_write(self):
        with flag(**ON):
            self.assertIs(get_app_tool(NAME), DELETE_TASK_ATTACHMENT)
            self.assertIs(_app_tool(NAME), DELETE_TASK_ATTACHMENT)
            names = [d.name for d in _build_tool_declarations()]
            self.assertEqual(names[-1], NAME)
            self.assertEqual(names.count(NAME), 1)
            self.assertEqual({t.name for t in app_write_tools()}, registered_writes() | {NAME})

    def test_the_declaration_says_it_asks_first_and_is_destructive(self):
        with flag(**ON):
            declared = {d.name: d for d in _build_tool_declarations()}[NAME]
        self.assertIn("REQUIRES USER APPROVAL", declared.description)
        self.assertIn("destructive", declared.description)
        self.assertEqual(
            set(declared.parameters_schema["properties"]), {"task_id", "attachment_id"}
        )
        self.assertEqual(declared.parameters_schema["required"], ["task_id", "attachment_id"])

    def test_the_read_tier_hides_it_and_the_act_tier_keeps_it(self):
        with flag(**ON):
            self.assertIn(NAME, tool_tiers.disabled_tools_for_level("read"))
            self.assertNotIn(NAME, tool_tiers.disabled_tools_for_level("act"))
            self.assertNotIn(NAME, tool_tiers.disabled_tools_for_level("organize"))
            declared = {
                d.name
                for d in _build_tool_declarations(tool_tiers.disabled_tools_for_level("read"))
            }
        self.assertNotIn(NAME, declared)
        self.assertIn(NAME, tool_tiers.SINGLE_WRITE_TOOLS)

    def test_the_digest_has_no_approver_so_it_never_sees_it(self):
        with flag(**ON):
            self.assertIn(NAME, _digest_disabled_tools())

    def test_the_critique_pass_is_read_only(self):
        seen = {}

        def fake_loop(**kwargs):
            seen["disabled"] = kwargs["disabled_tools"]

        with (
            flag(**ON),
            mock.patch.object(controller, "_drive_loop", side_effect=fake_loop),
        ):
            _critique_with_retrieval(
                loop1_events=[],
                draft="draft",
                messages=[],
                ctx=ToolContext(team_id="t", user_id="u"),
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                disabled_tools=None,
                system_extra=None,
                trace_hook=None,
            )
        self.assertIn(NAME, seen["disabled"])
        self.assertLessEqual(registered_writes(), seen["disabled"])

    def test_the_operator_kill_switch_hides_it_with_the_flag_on(self):
        with flag(AGENT_DISABLED_TOOLS=frozenset({NAME}), **ON):
            names = [d.name for d in _build_tool_declarations()]
        self.assertNotIn(NAME, names)


class ResumeTests(TestCase):
    """An approval token must not outlive the grant, for a flag-gated tool too: the resume path
    runs the tool the approved call names, so it asks the same lookup the loop does."""

    def _paused(self, ctx):
        run = AgentRun.objects.create(
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            query="delete the file",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        step = AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name=NAME,
            arguments_json={"task_id": 1, "attachment_id": 1},
            summary=PENDING_APPROVAL_MARKER,
        )
        return run, step

    def _resume(self, *, disabled=None, **flags):
        ctx = ToolContext(team_id="t1", user_id="u1")
        run, step = self._paused(ctx)
        events = []
        fake = SimpleNamespace(
            name=NAME,
            requires_approval=True,
            run=mock.Mock(return_value={"__summary__": "deleted"}),
        )
        entry = (PARITY_FLAG, fake)
        with (
            flag(**flags),
            mock.patch.dict(FLAGGED_REGISTRY, {NAME: entry}),
            mock.patch.object(controller, "_drive_loop", return_value=None),
        ):
            resume_agent(run, "approve", ctx, events.append, disabled_tools=disabled)
        step.refresh_from_db()
        return fake, events, step

    def test_with_the_flag_on_the_approved_call_runs(self):
        fake, events, _ = self._resume(**ON)
        fake.run.assert_called_once()
        self.assertIn("tool_call_result", {e["type"] for e in events})

    def test_an_approval_paused_before_the_flag_went_off_is_vetoed_as_unknown(self):
        fake, events, step = self._resume(**OFF)
        fake.run.assert_not_called()
        errors = [e["error"] for e in events if e["type"] == "tool_call_error"]
        self.assertEqual(errors, [f"Unknown tool: {NAME}"])
        self.assertTrue(step.error)

    def test_an_approval_for_a_tool_the_tier_no_longer_gives_is_vetoed(self):
        fake, events, _ = self._resume(disabled={NAME}, **ON)
        fake.run.assert_not_called()
        errors = [e["error"] for e in events if e["type"] == "tool_call_error"]
        self.assertIn("no longer available", errors[0])

    def test_an_approval_for_a_tool_the_operator_switched_off_is_vetoed(self):
        fake, events, _ = self._resume(AGENT_DISABLED_TOOLS=frozenset({NAME}), **ON)
        fake.run.assert_not_called()
        self.assertTrue([e for e in events if e["type"] == "tool_call_error"])

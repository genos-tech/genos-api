"""Tests for the AGENCY ladder (`agent/tool_tiers.py`).

The UX tier model's tool gate (genos-docs
operations/UX_TIER_MODEL_PLAN.md §4). Three properties pinned here:

  - Classification is EXHAUSTIVE and test-enforced: every
    `requires_approval` tool sits in exactly one of
    {SINGLE_WRITE_TOOLS, COMPOSITE_WRITE_TOOLS}. Adding a write tool
    without classifying it fails CI instead of silently landing in the
    wrong tier.
  - Unclassified tools fail CLOSED at runtime: a new write tool is
    hidden from `read` AND from `act` — only `organize` gets it.
  - The `/decide/` resume leg honours the same gate: an approval
    issued before a downgrade must not execute a tool the user no
    longer has, and the continued loop redeclares the gated surface.
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from origin.search_engine.agent import controller, tool_tiers
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER, resume_agent
from origin.search_engine.agent.tool_tiers import (
    COMPOSITE_WRITE_TOOLS,
    SINGLE_WRITE_TOOLS,
    disabled_tools_for_level,
    tier_disabled_tools,
)
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.models import AgentRun, AgentStep


def _write_tools() -> set[str]:
    return {t.name for t in REGISTRY.values() if t.requires_approval}


class ClassificationTests(SimpleTestCase):
    def test_every_write_tool_classified_exactly_once(self):
        writes = _write_tools()
        self.assertEqual(
            writes,
            set(SINGLE_WRITE_TOOLS) | set(COMPOSITE_WRITE_TOOLS),
            "a requires_approval tool is missing from (or stale in) the "
            "SINGLE/COMPOSITE classification in agent/tool_tiers.py — "
            "classify it before it ships in the wrong tier",
        )
        self.assertFalse(
            SINGLE_WRITE_TOOLS & COMPOSITE_WRITE_TOOLS,
            "a tool cannot be both single and composite",
        )

    def test_classified_names_exist_and_require_approval(self):
        # Catches renames: a stale name here would silently weaken the
        # act gate (the remainder derivation would still be safe, but
        # the classification would no longer describe reality).
        for name in SINGLE_WRITE_TOOLS | COMPOSITE_WRITE_TOOLS:
            self.assertIn(name, REGISTRY, f"{name} not in REGISTRY")
            self.assertTrue(
                REGISTRY[name].requires_approval,
                f"{name} is classified as a write tool but has requires_approval=False",
            )


class LevelSetTests(SimpleTestCase):
    def test_organize_disables_nothing(self):
        self.assertEqual(disabled_tools_for_level("organize"), set())

    def test_read_hides_exactly_the_write_set(self):
        self.assertEqual(disabled_tools_for_level("read"), _write_tools())

    def test_act_hides_exactly_the_composites(self):
        self.assertEqual(disabled_tools_for_level("act"), set(COMPOSITE_WRITE_TOOLS))

    def test_unclassified_new_write_tool_fails_closed(self):
        rogue = SimpleNamespace(name="frob_everything", requires_approval=True)
        with mock.patch.dict(REGISTRY, {"frob_everything": rogue}):
            self.assertIn("frob_everything", disabled_tools_for_level("read"))
            self.assertIn(
                "frob_everything",
                disabled_tools_for_level("act"),
                "an unclassified write tool must NOT be handed to `act`",
            )
            self.assertNotIn("frob_everything", disabled_tools_for_level("organize"))

    def test_read_level_declarations_are_the_read_tools(self):
        # Through the real declaration builder — the same path the ask
        # view's union feeds.
        declared = {
            d.name for d in controller._build_tool_declarations(disabled_tools_for_level("read"))
        }
        self.assertEqual(declared, set(REGISTRY) - _write_tools())

    def test_tier_disabled_tools_maps_the_quota_level(self):
        with mock.patch.object(tool_tiers, "get_agent_tool_level", return_value="read"):
            self.assertEqual(tier_disabled_tools("u1"), _write_tools())
        with mock.patch.object(tool_tiers, "get_agent_tool_level", return_value="organize"):
            self.assertEqual(tier_disabled_tools("u1"), set())


class TierSystemExtraTests(SimpleTestCase):
    def test_read_level_gets_the_draft_and_offer_branch(self):
        with mock.patch.object(tool_tiers, "get_agent_tool_level", return_value="read"):
            extra = tool_tiers.tier_system_extra("u1")
        self.assertIn("READ-ONLY", extra)
        self.assertIn("ready-to-paste", extra)
        # The upgrade mention must stay a one-liner, not a sales pitch.
        self.assertIn("at most once per conversation", extra)

    def test_other_levels_add_nothing(self):
        for level in ("act", "organize"):
            with mock.patch.object(tool_tiers, "get_agent_tool_level", return_value=level):
                self.assertIsNone(tool_tiers.tier_system_extra("u1"), level)


class ResumeVetoTests(TestCase):
    """An approval token must not outlive the grant (plan trap #1)."""

    def _paused_run(self, tool_name="create_task"):
        run = AgentRun.objects.create(
            team_id="t1",
            user_id="u1",
            query="make me a task",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        step = AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name=tool_name,
            arguments_json={"title": "x"},
            summary=PENDING_APPROVAL_MARKER,
        )
        return run, step

    @staticmethod
    def _fake_tool(result=None):
        # Tool is a frozen dataclass, so swap the REGISTRY entry for a
        # mockable stand-in instead of patching its attributes.
        return SimpleNamespace(
            name="create_task",
            requires_approval=True,
            run=mock.Mock(return_value=result or {"__summary__": "created"}),
        )

    def test_disabled_pending_tool_is_vetoed_not_executed(self):
        run, step = self._paused_run()
        events = []
        ctx = ToolContext(team_id="t1", user_id="u1")
        fake = self._fake_tool()
        with (
            mock.patch.object(controller, "_drive_loop", return_value=None) as loop,
            mock.patch.dict(REGISTRY, {"create_task": fake}),
        ):
            result = resume_agent(
                run,
                "approve",
                ctx,
                events.append,
                disabled_tools={"create_task"},
            )
        self.assertIsNone(result)
        fake.run.assert_not_called()
        errors = [e for e in events if e["type"] == "tool_call_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("no longer available", errors[0]["error"])
        self.assertNotIn("tool_call_result", {e["type"] for e in events})
        step.refresh_from_db()
        self.assertTrue(step.error)
        # The continued loop still runs — with the SAME gated surface.
        self.assertEqual(loop.call_args.kwargs["disabled_tools"], {"create_task"})

    def test_allowed_pending_tool_still_executes(self):
        run, step = self._paused_run()
        events = []
        ctx = ToolContext(team_id="t1", user_id="u1")
        fake = self._fake_tool()
        with (
            mock.patch.object(controller, "_drive_loop", return_value=None) as loop,
            mock.patch.dict(REGISTRY, {"create_task": fake}),
        ):
            resume_agent(
                run,
                "approve",
                ctx,
                events.append,
                disabled_tools={"update_tasks_bulk"},  # a gate that doesn't cover this call
            )
        fake.run.assert_called_once()
        self.assertIn("tool_call_result", {e["type"] for e in events})
        self.assertEqual(loop.call_args.kwargs["disabled_tools"], {"update_tasks_bulk"})

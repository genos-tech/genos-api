"""Tests for the REACH gate (UX tier model §7 — integrations).

Enforcement is the same declaration union as the agency ladder: the
tier's `integrations` allowlist disables the COMPLEMENT of the mapped
tools — remove the tool, never let it error. `web_search._run` keeps a
defence-in-depth ToolError for any path that bypasses the declaration
union, plus the credits-predicate unification pinned at the bottom.
"""

from unittest import mock

from django.test import override_settings

from origin.search_engine import quota
from origin.search_engine.agent import tool_tiers
from origin.search_engine.agent.tools import FLAGGED_REGISTRY, REGISTRY, app_write_tools, web_search
from origin.search_engine.agent.tools.base import ToolContext, ToolError

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

_ALL_INTEGRATION_TOOLS = frozenset().union(*tool_tiers.INTEGRATION_TOOLS.values())


def _quotas_with_integrations(integrations):
    free = {**TEST_QUOTAS["free"]}
    if integrations is None:
        free.pop("integrations", None)
    else:
        free["integrations"] = integrations
    return {**TEST_QUOTAS, "free": free}


class ReachGateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])
        self.uid = str(self.user.id)

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def test_integration_tool_names_exist_in_the_registry(self):
        # Rename guard: a stale name here would silently stop gating
        # the renamed tool.
        for name in _ALL_INTEGRATION_TOOLS:
            # A flag-gated tool (`get_calendar_event`, MIP-1059) is filed beside the registry.
            self.assertTrue(name in REGISTRY or name in FLAGGED_REGISTRY, name)

    def test_empty_allowlist_disables_every_integration_tool(self):
        with override_settings(
            SEARCH_ENGINE=_search_engine_with_quotas(_quotas_with_integrations([]))
        ):
            self.assertEqual(
                tool_tiers.reach_disabled_tools(self.uid), set(_ALL_INTEGRATION_TOOLS)
            )

    def test_the_flag_gated_calendar_read_is_held_by_the_google_calendar_reach(self):
        # MIP-1059: a Free member (no integrations) must not be declared `get_calendar_event` while the
        # parity flag is on, as they are not declared the other calendar tools.
        from django.conf import settings  # noqa: PLC0415

        from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: PLC0415

        with override_settings(
            SEARCH_ENGINE=_search_engine_with_quotas(_quotas_with_integrations([]))
        ):
            disabled = tool_tiers.reach_disabled_tools(self.uid)
            self.assertIn("get_calendar_event", disabled)
            with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": True}):
                declared = {d.name for d in _build_tool_declarations(disabled)}
        self.assertNotIn("get_calendar_event", declared)
        self.assertNotIn("list_calendar_events", declared)
        with override_settings(
            SEARCH_ENGINE=_search_engine_with_quotas(
                _quotas_with_integrations(["web", "google_calendar"])
            )
        ):
            self.assertNotIn("get_calendar_event", tool_tiers.reach_disabled_tools(self.uid))
            with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": True}):
                allowed = {
                    d.name
                    for d in _build_tool_declarations(tool_tiers.reach_disabled_tools(self.uid))
                }
        self.assertIn("get_calendar_event", allowed)

    def test_the_read_tier_keeps_the_calendar_read_and_hides_no_new_write(self):
        # `get_calendar_event` is a read: the agency ladder never hides it, and it is no write.
        from django.conf import settings  # noqa: PLC0415

        with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": True}):
            self.assertNotIn("get_calendar_event", tool_tiers.disabled_tools_for_level("read"))
            self.assertNotIn("get_calendar_event", {t.name for t in app_write_tools()})

    def test_partial_allowlist_disables_the_complement(self):
        with override_settings(
            SEARCH_ENGINE=_search_engine_with_quotas(
                _quotas_with_integrations(["web", "google_calendar"])
            )
        ):
            self.assertEqual(
                tool_tiers.reach_disabled_tools(self.uid),
                set(tool_tiers.INTEGRATION_TOOLS["github"]),
            )

    def test_full_allowlist_and_missing_key_disable_nothing(self):
        for integrations in (["web", "google_calendar", "github"], None):
            with override_settings(
                SEARCH_ENGINE=_search_engine_with_quotas(
                    _quotas_with_integrations(integrations)
                )
            ):
                self.assertEqual(tool_tiers.reach_disabled_tools(self.uid), set())

    def test_disabled_tools_for_user_unions_all_three_pillars(self):
        rigged = {
            **TEST_QUOTAS,
            "free": {
                **TEST_QUOTAS["free"],
                "agent_tool_level": "read",
                "agent_memory": "none",
                "integrations": [],
            },
        }
        writes = {t.name for t in REGISTRY.values() if t.requires_approval}
        with override_settings(SEARCH_ENGINE=_search_engine_with_quotas(rigged)):
            self.assertEqual(
                tool_tiers.disabled_tools_for_user(self.uid),
                writes | {"search_past_conversations"} | set(_ALL_INTEGRATION_TOOLS),
            )


class WebSearchDefenceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])
        self.ctx = ToolContext(team_id=str(self.team.pk), user_id=str(self.user.id))

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    @staticmethod
    def _se(integrations, **flags):
        se = _search_engine_with_quotas(_quotas_with_integrations(integrations))
        se["TAVILY_API_KEY"] = ""
        se.update(flags)
        return se

    def test_tier_without_web_gets_a_tool_error(self):
        with override_settings(SEARCH_ENGINE=self._se([])):
            with self.assertRaisesMessage(ToolError, "not available on this plan"):
                REGISTRY["search_web"].run({"query": "x"}, self.ctx)

    def test_tier_with_web_passes_the_defence(self):
        # Falls through to the unconfigured-key error — i.e. past the
        # reach gate.
        with override_settings(
            SEARCH_ENGINE=self._se(
                ["web"], AI_CREDITS_AUTHORITATIVE=False, AI_CREDITS_SHADOW=False
            )
        ):
            with self.assertRaisesMessage(ToolError, "not configured"):
                REGISTRY["search_web"].run({"query": "x"}, self.ctx)

    def test_daily_cap_enforced_when_credits_are_only_half_configured(self):
        # The unified predicate: AUTHORITATIVE without SHADOW is the
        # misconfigured state — the raw-setting read used to drop the
        # cap here while the ask gate still enforced daily asks.
        with override_settings(
            SEARCH_ENGINE=self._se(
                ["web"], AI_CREDITS_AUTHORITATIVE=True, AI_CREDITS_SHADOW=False
            )
        ):
            with mock.patch.object(
                web_search, "check_remaining", return_value=(False, 10, 10)
            ):
                with self.assertRaisesMessage(ToolError, "web searches for today"):
                    REGISTRY["search_web"].run({"query": "x"}, self.ctx)

    def test_daily_cap_skipped_when_credits_are_authoritative(self):
        with override_settings(
            SEARCH_ENGINE=self._se(
                ["web"], AI_CREDITS_AUTHORITATIVE=True, AI_CREDITS_SHADOW=True
            )
        ):
            with mock.patch.object(web_search, "check_remaining") as gate:
                with self.assertRaisesMessage(ToolError, "not configured"):
                    REGISTRY["search_web"].run({"query": "x"}, self.ctx)
            gate.assert_not_called()

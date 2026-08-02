"""MCP is a Pro-and-up capability.

Three things are worth pinning separately, because they fail in
different directions.

**The gate holds.** A Free or Core key must not reach the tools, and it
must be told why in a way that names the fix — a bare 403 on a wire
protocol surfaces to the user as "the server is broken".

**The handshake stays open.** `initialize` has to succeed even for a
tier that cannot use MCP, or the client reports a connection failure and
never displays the message explaining the real reason.

**It fails OPEN.** The permissive default is the house contract for
every capability key except `digest_cadence`, and it is easy to
"correct" into a fail-closed check that looks more secure and quietly
disconnects paying customers during a Redis incident.
"""

from __future__ import annotations

from django.conf import settings
from django.test import override_settings

from origin.models.common.api_key_models import (
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import quota
from origin.tests.test_base import BaseAPITestCase

MCP = "/api/public/v1/mcp"


def _quotas(**per_tier):
    """`TIER_QUOTAS` with `mcp_enabled` forced per tier."""
    base = settings.SEARCH_ENGINE["TIER_QUOTAS"]
    merged = {t: {**cfg} for t, cfg in base.items()}
    for tier, value in per_tier.items():
        if value is _MISSING:
            merged[tier].pop("mcp_enabled", None)
        else:
            merged[tier]["mcp_enabled"] = value
    return {**settings.SEARCH_ENGINE, "TIER_QUOTAS": merged}


class _Missing:
    pass


_MISSING = _Missing()


class McpTierGateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Gate",
            code="GAT",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Anything",
            status="Open",
            project_task_number=1,
        )
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        # The tier is cached per user for 60s; a test that changes the
        # table without clearing it reads the previous verdict.
        quota.invalidate_effective_tier([str(self.user.id)])

    def tearDown(self):
        quota.invalidate_effective_tier([str(self.user.id)])
        super().tearDown()

    def _rpc(self, method, params=None, rid=1):
        body = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            body["params"] = params
        return self.client.post(MCP, body, format="json")

    def _as_tier(self, enabled):
        """Force the calling user's tier to allow/refuse MCP."""
        quota.invalidate_effective_tier([str(self.user.id)])
        tier = quota.get_effective_tier(str(self.user.id))
        return override_settings(SEARCH_ENGINE=_quotas(**{tier: enabled}))

    # -- the gate holds --

    def test_a_tier_without_mcp_cannot_list_tools(self):
        with self._as_tier(False):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = self._rpc("tools/list")
        self.assertEqual(res.status_code, 403)
        self.assertIn("error", res.data)

    def test_a_tier_without_mcp_cannot_call_a_tool(self):
        with self._as_tier(False):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = self._rpc(
                "tools/call",
                {"name": "get_task", "arguments": {"task_id": self.task.task_id}},
            )
        self.assertEqual(res.status_code, 403)
        # A PROTOCOL error, not a tool result carrying `isError`: the
        # model cannot fix a subscription by retrying with different
        # arguments, and dressing it as a tool failure invites it to.
        self.assertIn("error", res.data)
        self.assertNotIn("result", res.data)

    def test_the_refusal_names_the_fix_and_what_still_works(self):
        """A bare 403 on a wire protocol reaches the user as "the server
        is broken". This message is the only place they learn otherwise."""
        with self._as_tier(False):
            quota.invalidate_effective_tier([str(self.user.id)])
            message = self._rpc("tools/list").data["error"]["message"]
        self.assertIn("Pro", message)
        self.assertIn("Settings", message)
        # The REST API is NOT gated, and someone told "your plan doesn't
        # include this" will otherwise assume their key is dead.
        self.assertIn("/api/public/v1/", message)

    # -- the handshake stays open --

    def test_the_handshake_still_succeeds_on_an_ungated_tier(self):
        """Otherwise the client reports a connection failure and never
        gets far enough to show the message above."""
        with self._as_tier(False):
            quota.invalidate_effective_tier([str(self.user.id)])
            init = self._rpc("initialize", {"protocolVersion": "2025-11-25"}, rid=0)
            ping = self._rpc("ping")
        self.assertEqual(init.status_code, 200)
        self.assertIn("result", init.data)
        self.assertEqual(ping.status_code, 200)

    # -- an entitled tier is unaffected --

    def test_an_entitled_tier_works_exactly_as_before(self):
        with self._as_tier(True):
            quota.invalidate_effective_tier([str(self.user.id)])
            listed = self._rpc("tools/list")
            called = self._rpc(
                "tools/call",
                {"name": "get_task", "arguments": {"task_id": self.task.task_id}},
            )
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(listed.data["result"]["tools"])
        self.assertEqual(called.status_code, 200)
        self.assertFalse(called.data["result"]["isError"])


class McpTierFailOpenTests(BaseAPITestCase):
    """The permissive default, isolated from HTTP.

    `mcp_enabled` gates a paid feature, which makes fail-open look like a
    bug to anyone reading it cold — so it gets its own test saying that
    it is the intended contract, and why.
    """

    def test_a_missing_key_allows_mcp(self):
        # The real scenario: a `TIER_QUOTAS_JSON` ops override written
        # before this key existed. Every tier in it would lack
        # `mcp_enabled`, and a fail-CLOSED read would cut off every
        # paying customer at once, silently.
        uid = str(self.user.id)
        quota.invalidate_effective_tier([uid])
        tier = quota.get_effective_tier(uid)
        with override_settings(SEARCH_ENGINE=_quotas(**{tier: _MISSING})):
            quota.invalidate_effective_tier([uid])
            self.assertTrue(quota.get_mcp_enabled(uid))

    def test_a_nonsense_value_allows_mcp(self):
        uid = str(self.user.id)
        quota.invalidate_effective_tier([uid])
        tier = quota.get_effective_tier(uid)
        with override_settings(SEARCH_ENGINE=_quotas(**{tier: "yes please"})):
            quota.invalidate_effective_tier([uid])
            self.assertTrue(quota.get_mcp_enabled(uid))

    def test_an_explicit_false_still_refuses(self):
        """Fail-open must not mean "never refuses" — the whole gate would
        be decorative."""
        uid = str(self.user.id)
        quota.invalidate_effective_tier([uid])
        tier = quota.get_effective_tier(uid)
        with override_settings(SEARCH_ENGINE=_quotas(**{tier: False})):
            quota.invalidate_effective_tier([uid])
            self.assertFalse(quota.get_mcp_enabled(uid))


class McpTierIsAdvertisedTests(BaseAPITestCase):
    """The plans page can only show a row the server sends."""

    def test_the_plans_payload_carries_mcp_enabled(self):
        res = self.client.get("/api/v2/billing/plans/")
        self.assertEqual(res.status_code, 200)
        by_tier = {t["tier"]: t["limits"] for t in res.data["tiers"]}
        for tier in ("free", "core", "pro", "max", "enterprise"):
            self.assertIn("mcp_enabled", by_tier[tier], f"{tier} is missing the key")
        # The actual product decision, asserted where a reader looks for
        # it rather than only in a settings table.
        self.assertFalse(by_tier["free"]["mcp_enabled"])
        self.assertFalse(by_tier["core"]["mcp_enabled"])
        self.assertTrue(by_tier["pro"]["mcp_enabled"])
        self.assertTrue(by_tier["max"]["mcp_enabled"])
        self.assertTrue(by_tier["enterprise"]["mcp_enabled"])

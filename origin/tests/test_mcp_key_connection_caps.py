"""How many API keys and connected AI apps a person may have (MIP-1367, phase 3). DARK-SHIPPED.

A limit on what a person CREATES, so the properties are about creation only:

* it is dark: `off` (default) and `enforce` with every live value `None` create exactly as before and
  query nothing for the count;
* over the limit a NEW key or connection is refused with the 429 `limit_reached` body the plan limits
  use everywhere (`{error, limit_reached, used, limit, category}`), and nothing is written;
* the keys and connections that exist keep working whatever the limit becomes (hide, never delete);
* revoked and expired keys, other people's keys and a connection the person already has active do
  not count, and re-consenting to an active connection is not a new one;
* shadow logs and changes nothing; a count that fails never blocks a creation.
"""

from __future__ import annotations

import os
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from oauth2_provider.models import Application

from origin.models.common.api_key_models import ApiKey
from origin.models.common.mcp_connection_models import McpConnection
from origin.search_engine import quota
from origin.services import mcp_tier_limits as limits
from origin.tests.test_api_keys import KEYS_URL
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_consent import ConsentBase

LIMIT_BODY_KEYS = {"error", "limit_reached", "used", "limit", "category"}


def _env(mode=None):
    env = {k: v for k, v in os.environ.items() if k != limits.MODE_ENV}
    if mode is not None:
        env[limits.MODE_ENV] = mode
    return mock.patch.dict(os.environ, env, clear=True)


class _Rigs:
    tier = "core"

    def rig(self, tier=None, **values):
        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table[tier or self.tier].update(values)
        ctx = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table})
        ctx.enable()
        self.addCleanup(ctx.disable)
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def set_tier(self, user, tier):
        user.tier = tier
        user.save()
        quota.invalidate_effective_tier([str(user.id)])

    def proposed(self, **values):
        table = {t: dict(row) for t, row in limits.MCP_TIER_LIMITS_PROPOSED.items()}
        table[self.tier].update(values)
        patcher = mock.patch.object(limits, "MCP_TIER_LIMITS_PROPOSED", table)
        patcher.start()
        self.addCleanup(patcher.stop)


class ApiKeyCaps(_Rigs, BaseAPITestCase):
    mode = limits.ENFORCE

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        env = _env(self.mode)
        env.start()
        self.addCleanup(env.stop)
        self.set_tier(self.user, self.tier)
        self.authenticate()

    def create(self, name="k", **extra):
        return self.client.post(KEYS_URL, {"name": name, **extra}, format="json")

    _n = 0

    def make_keys(self, n, user=None, **fields):
        type(self)._n += 1
        for i in range(n):
            ApiKey.objects.create(
                user=user or self.user,
                name=f"k{i}",
                key_hash=f"{(user or self.user).id}-{i}-{sorted(fields)}-{len(fields)}-{self._n}",
                prefix="gnos_x",
                **fields,
            )


class TestDarkMeansNothing(ApiKeyCaps):
    mode = None

    def test_off_creates_any_number_even_with_the_live_value_flipped(self):
        self.rig(mcp_max_api_keys=1)
        for _ in range(4):
            self.assertEqual(self.create().status_code, 201)
        self.assertEqual(ApiKey.objects.filter(user=self.user).count(), 4)

    def test_off_counts_nothing(self):
        self.rig(mcp_max_api_keys=1)
        with mock.patch("origin.views.common.api_key_views._active_key_count") as count:
            self.assertEqual(self.create().status_code, 201)
        count.assert_not_called()


class TestEnforceWithNoLiveValue(ApiKeyCaps):
    def test_enforce_with_the_table_as_shipped_creates_exactly_as_before(self):
        with mock.patch("origin.views.common.api_key_views._active_key_count") as count:
            for _ in range(5):
                res = self.create()
                self.assertEqual(res.status_code, 201)
        count.assert_not_called()
        self.assertEqual(
            set(res.json()),
            {
                "id",
                "name",
                "prefix",
                "scope",
                "allowDestructive",
                "teamId",
                "lastUsedAt",
                "expiresAt",
                "revokedAt",
                "createdAt",
                "key",
            },
        )


class TestKeyLimit(ApiKeyCaps):
    def test_the_key_over_the_limit_is_a_429_limit_reached_and_nothing_is_written(self):
        self.rig(mcp_max_api_keys=2)
        self.assertEqual(self.create("a").status_code, 201)
        self.assertEqual(self.create("b").status_code, 201)
        res = self.create("c")
        self.assertEqual(res.status_code, 429)
        body = res.json()
        self.assertEqual(set(body), LIMIT_BODY_KEYS)
        self.assertEqual(
            {k: body[k] for k in ("limit_reached", "used", "limit", "category")},
            {"limit_reached": True, "used": 2, "limit": 2, "category": "mcp_api_keys"},
        )
        self.assertIn("Your plan allows 2 active API keys, and you have 2.", body["error"])
        self.assertIn("Settings → Developer", body["error"])
        self.assertIn("existing keys keep working", body["error"])
        self.assertEqual(ApiKey.objects.filter(user=self.user).count(), 2)

    def test_the_existing_keys_keep_working_and_listing_is_unchanged(self):
        self.make_keys(3)
        self.rig(mcp_max_api_keys=1)
        self.assertEqual(self.create().status_code, 429)
        listed = self.client.get(KEYS_URL)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["api_keys"]), 3)

    def test_revoking_one_makes_room(self):
        self.rig(mcp_max_api_keys=1)
        first = self.create("a").json()
        self.assertEqual(self.create("b").status_code, 429)
        self.assertEqual(self.client.delete(f"{KEYS_URL}{first['id']}/").status_code, 200)
        self.assertEqual(self.create("c").status_code, 201)

    def test_revoked_and_expired_keys_do_not_count(self):
        self.make_keys(2, revoked_at=timezone.now())
        self.make_keys(2, expires_at=timezone.now() - timedelta(days=1))
        self.rig(mcp_max_api_keys=1)
        self.assertEqual(self.create().status_code, 201)

    def test_another_persons_keys_do_not_count_and_she_has_her_own_room(self):
        self.make_keys(5, user=self.user2)
        self.rig(mcp_max_api_keys=1)
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.create().status_code, 429)

    def test_team_keys_count_towards_the_creators_total(self):
        self.rig(mcp_max_api_keys=1)
        self.assertEqual(self.create("t", team_id=str(self.team.team_id)).status_code, 201)
        self.assertEqual(self.create("p").status_code, 429)

    def test_a_request_that_is_invalid_is_still_a_400_not_a_limit(self):
        self.make_keys(3)
        self.rig(mcp_max_api_keys=1)
        self.assertEqual(self.client.post(KEYS_URL, {"name": ""}, format="json").status_code, 400)
        self.assertEqual(
            self.client.post(KEYS_URL, {"name": "x", "scope": "nope"}, format="json").status_code,
            400,
        )

    def test_the_limit_is_the_plans_own_so_a_plan_without_one_is_unlimited(self):
        self.set_tier(self.user, "pro")
        self.rig(tier="core", mcp_max_api_keys=1)
        for _ in range(3):
            self.assertEqual(self.create().status_code, 201)

    def test_free_keeps_its_rest_keys_unlimited(self):
        self.set_tier(self.user, "free")
        self.assertIsNone(limits.MCP_TIER_LIMITS_PROPOSED["free"][limits.MAX_API_KEYS])
        for _ in range(4):
            self.assertEqual(self.create().status_code, 201)

    def test_a_tier_lookup_failure_is_cores_limit(self):
        self.set_tier(self.user, "pro")
        self.rig(tier="core", mcp_max_api_keys=1)
        with mock.patch.object(quota, "get_effective_tier", side_effect=RuntimeError("db")):
            self.assertEqual(self.create().status_code, 201)
            self.assertEqual(self.create().status_code, 429)

    def test_a_count_that_fails_never_blocks_the_creation(self):
        self.rig(mcp_max_api_keys=1)
        with mock.patch(
            "origin.views.common.api_key_views._active_key_count", side_effect=RuntimeError("db")
        ):
            for _ in range(3):
                self.assertEqual(self.create().status_code, 201)

    def test_a_read_only_key_still_cannot_reach_this_surface(self):
        # The management surface is JWT only; the limit did not change that.
        self.rig(mcp_max_api_keys=5)
        self.client.credentials(HTTP_AUTHORIZATION="ApiKey gnos_nope")
        self.assertIn(self.create().status_code, (401, 403))


class TestKeyShadow(ApiKeyCaps):
    mode = limits.SHADOW

    def test_shadow_logs_and_creates(self):
        self.proposed(mcp_max_api_keys=1)
        self.assertEqual(self.create().status_code, 201)
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            res = self.create("b")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["mcp.tier_would_limit tier=core category=mcp_api_keys limit=1 used=1 tool=-"],
        )
        self.assertEqual(ApiKey.objects.filter(user=self.user).count(), 2)

    def test_shadow_does_not_read_the_live_value(self):
        self.rig(mcp_max_api_keys=1)
        self.make_keys(2)
        with self.assertNoLogs("origin.services.mcp_tier_limits", "INFO"):
            self.assertEqual(self.create().status_code, 201)


class TestTheProposedNumbers(BaseAPITestCase):
    def test_key_and_connection_limits_are_three_ten_twenty_five(self):
        for key in (limits.MAX_API_KEYS, limits.MAX_CONNECTIONS):
            self.assertEqual(
                [limits.MCP_TIER_LIMITS_PROPOSED[t][key] for t in ("core", "pro", "max")],
                [3, 10, 25],
            )
            self.assertIsNone(limits.MCP_TIER_LIMITS_PROPOSED["enterprise"][key])


# ------------------------------------------------------------------------------------------
# Connected AI apps (the consent step)
# ------------------------------------------------------------------------------------------


class ConnectionCaps(_Rigs, ConsentBase):
    mode = limits.ENFORCE

    def setUp(self):
        super().setUp()
        env = _env(self.mode)
        env.start()
        self.addCleanup(env.stop)
        self.set_tier(self.user, self.tier)

    def another_app(self, n):
        return Application.objects.create(
            client_id=f"app-{n}",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
            name=f"App {n}",
        )

    def connect(self, n, user=None, revoked=False):
        return McpConnection.objects.create(
            user=user or self.user,
            application=self.another_app(n),
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            revoked_at=timezone.now() if revoked else None,
        )


class TestConnectionsDark(ConnectionCaps):
    mode = None

    def test_off_connects_any_number_and_counts_nothing(self):
        self.rig(mcp_max_connections=0)
        for n in range(3):
            self.connect(n)
        with mock.patch.object(limits, "check_new_credential", side_effect=AssertionError("asked")):
            res = self.decide()
        self.assertEqual(res.status_code, 200, res.content)


class TestConnectionLimit(ConnectionCaps):
    def test_a_new_connection_over_the_limit_is_a_429_and_nothing_is_written(self):
        self.rig(mcp_max_connections=2)
        self.connect(1)
        self.connect(2)
        before = McpConnection.objects.count()
        res = self.decide()
        self.assertEqual(res.status_code, 429, res.content)
        body = res.json()
        self.assertEqual(
            {k: body[k] for k in ("error", "limit_reached", "used", "limit", "category")},
            {
                "error": "plan_limit",
                "limit_reached": True,
                "used": 2,
                "limit": 2,
                "category": "mcp_connections",
            },
        )
        self.assertIn(
            "Your plan allows 2 connected AI apps, and you have 2.", body["error_description"]
        )
        self.assertIn("Settings → Connected apps", body["error_description"])
        self.assertEqual(McpConnection.objects.count(), before)

    def test_it_connects_under_the_limit(self):
        self.rig(mcp_max_connections=2)
        self.connect(1)
        self.assertEqual(self.decide().status_code, 200)

    def test_consenting_again_to_an_active_connection_is_not_a_new_one(self):
        self.rig(mcp_max_connections=1)
        self.assertEqual(self.decide().status_code, 200)
        self.assertEqual(self.decide().status_code, 200)
        self.assertEqual(McpConnection.objects.filter(user=self.user).count(), 1)

    def test_bringing_back_a_disconnected_one_counts_as_new(self):
        self.rig(mcp_max_connections=1)
        self.assertEqual(self.decide().status_code, 200)
        McpConnection.objects.update(revoked_at=timezone.now())
        self.connect(9)
        self.assertEqual(self.decide().status_code, 429)

    def test_revoked_connections_and_other_peoples_do_not_count(self):
        self.rig(mcp_max_connections=1)
        self.connect(1, revoked=True)
        self.connect(2, user=self.user2)
        self.assertEqual(self.decide().status_code, 200)

    def test_the_existing_connections_keep_working_when_the_limit_drops(self):
        self.assertEqual(self.decide().status_code, 200)
        self.rig(mcp_max_connections=0)
        connection = McpConnection.objects.get(user=self.user)
        self.assertTrue(connection.is_active)

    def test_a_denial_is_never_a_limit(self):
        self.rig(mcp_max_connections=0)
        self.connect(1)
        res = self.decide(allow=False)
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_plan_without_mcp_still_gets_its_own_403_first(self):
        self.rig(mcp_max_connections=0, mcp_enabled=False)
        res = self.decide()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"], "plan_required")

    def test_shadow_logs_and_connects(self):
        self.proposed(mcp_max_connections=1)
        self.connect(1)
        with _env("shadow"):
            with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
                res = self.decide()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["mcp.tier_would_limit tier=core category=mcp_connections limit=1 used=1 tool=-"],
        )
        self.assertEqual(McpConnection.objects.filter(user=self.user, revoked_at=None).count(), 2)


class TestTheCountFailingNeverBlocks(BaseAPITestCase):
    def test_a_count_that_raises_lets_the_creation_through_and_a_missing_limit_never_counts(self):
        def boom():
            raise RuntimeError("db")

        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table["free"][limits.MAX_CONNECTIONS] = 1
        with (
            _env("enforce"),
            override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table}),
        ):
            quota.invalidate_effective_tier([str(self.user.id)])
            self.assertIsNone(
                limits.check_new_credential(self.user.id, limits.MAX_CONNECTIONS, boom)
            )
            calls = []
            table["free"][limits.MAX_CONNECTIONS] = None
            self.assertIsNone(
                limits.check_new_credential(
                    self.user.id, limits.MAX_CONNECTIONS, lambda: calls.append(1) or 99
                )
            )
            self.assertEqual(calls, [])

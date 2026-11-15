"""Volume and reach limits per plan for MCP (MIP-1367, phase 3): DARK-SHIPPED.

What is defended, and the way each part can fail:

* **Nothing changes while it is dark.** `MCP_TIER_LIMITS_MODE` unset (`off`), and `enforce` with the
  table as shipped (every live value `None`), answer exactly as before, write no counter and run no
  extra query. A limit that leaks into the default path is the failure that matters most.
* **Shadow only watches.** It computes against `MCP_TIER_LIMITS_PROPOSED`, logs names and numbers,
  and changes nothing.
* **Each limit triggers with the exact answer shape** under a rigged tiny value (the way
  `TIER_QUOTAS_JSON` rigs one): transport limits as HTTP 429 + `Retry-After` + a JSON-RPC error
  whose `data` is `{limit_reached, used, limit, category}`, tool limits as an `isError` result.
* **The failure policy.** A tier that cannot be looked up is Core's, a missing key is unlimited, a
  broken value is Core's, a counter store that is down counts in this process.
* **Nobody else's bucket.** The per-minute counter is per credential, the totals per person.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from django_redis.exceptions import ConnectionInterrupted

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.search_engine import quota
from origin.services import mcp_tier_limits as limits
from origin.services import throttles
from origin.tests.test_mcp_chat_read import McpChatBase
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import limit_gate, tools
from origin.views.public.permissions import PublicApiThrottle

OK = {"content": [{"type": "text", "text": "ok"}], "isError": False}
CORE_PROPOSED = {
    "mcp_calls_per_min": 60,
    "mcp_calls_per_day": 3000,
    "mcp_search_daily": 100,
    "mcp_max_api_keys": 3,
    "mcp_max_connections": 3,
    "mcp_bulk_max_tasks": 10,
    "mcp_bulk_max_plan_tasks": 10,
    "mcp_list_page_max": 50,
    "mcp_apply_message_retention": True,
}


def _env(mode=None):
    env = {k: v for k, v in os.environ.items() if k != limits.MODE_ENV}
    if mode is not None:
        env[limits.MODE_ENV] = mode
    return mock.patch.dict(os.environ, env, clear=True)


class LimitsBase(McpBase):
    """A Core person with a write key, the mode switch under test control, a clean store."""

    mode = limits.ENFORCE
    tier = "core"

    def setUp(self):
        super().setUp()
        cache.clear()
        limits._local.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(limits._local.clear)
        # A cache error elsewhere opens a 5 s breaker that makes every throttle allow.
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        env = _env(self.mode)
        env.start()
        self.addCleanup(env.stop)
        self.set_tier(self.user, self.tier)
        self.raw = self._key(scope=SCOPE_WRITE)

    def set_tier(self, user, tier):
        user.tier = tier
        user.save()
        quota.invalidate_effective_tier([str(user.id)])

    def rig(self, tier=None, **values):
        """`TIER_QUOTAS` with these capability keys set on a tier (the way `TIER_QUOTAS_JSON` rigs
        one). Keys given as `_MISSING` are removed."""
        tier = tier or self.tier
        table = {t: dict(cfg) for t, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        for key, value in values.items():
            if value is _MISSING:
                table[tier].pop(key, None)
            else:
                table[tier][key] = value
        ctx = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table})
        ctx.enable()
        self.addCleanup(ctx.disable)
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def stub(self, result=OK):
        patcher = mock.patch.object(tools, "call", return_value=result)
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def call_ok(self, name="get_task", arguments=None, **kw):
        return self._call(name, arguments or {"task_id": "WRD-7"}, **kw)

    def counters(self):
        """Every counter key written under this feature (the in-memory cache keeps them)."""
        return [self._plain(k) for k in cache._cache if "mcp_tl" in str(k)]  # noqa: SLF001

    @staticmethod
    def _plain(internal_key):
        return str(internal_key).split(":", 2)[2]  # ":1:name" -> "name"

    def counter(self, kind):
        """The value of the one counter of this kind (`:day:`, `:srch:`, ...)."""
        keys = [k for k in self.counters() if kind in k and ":logged:" not in k]
        self.assertEqual(len(keys), 1, keys)
        return cache.get(keys[0])


class _Missing:
    pass


_MISSING = _Missing()


# ---------------------------------------------------------------------------------------------
# The numbers and the table
# ---------------------------------------------------------------------------------------------


class TestTheTable(McpBase):
    def test_the_proposed_numbers_are_the_ones_ken_approved(self):
        proposed = limits.MCP_TIER_LIMITS_PROPOSED
        self.assertEqual(proposed["core"], CORE_PROPOSED)
        self.assertEqual(
            proposed["pro"],
            {
                "mcp_calls_per_min": 120,
                "mcp_calls_per_day": 10000,
                "mcp_search_daily": 300,
                "mcp_max_api_keys": 10,
                "mcp_max_connections": 10,
                "mcp_bulk_max_tasks": 30,
                "mcp_bulk_max_plan_tasks": 20,
                "mcp_list_page_max": 100,
                "mcp_apply_message_retention": True,
            },
        )
        self.assertEqual(
            proposed["max"],
            {
                "mcp_calls_per_min": 240,
                "mcp_calls_per_day": 30000,
                "mcp_search_daily": 1000,
                "mcp_max_api_keys": 25,
                "mcp_max_connections": 25,
                "mcp_bulk_max_tasks": 30,
                "mcp_bulk_max_plan_tasks": 20,
                "mcp_list_page_max": 100,
                "mcp_apply_message_retention": True,
            },
        )
        # Free has no MCP and Enterprise is unlimited: nothing is limited for either.
        for tier in ("free", "enterprise"):
            self.assertEqual(set(proposed[tier].values()), {None}, tier)
        self.assertEqual(set(proposed), {"free", "core", "pro", "max", "enterprise"})

    def test_heavy_calls_weigh_five_and_are_the_four_ken_named(self):
        self.assertEqual(limits.HEAVY_WEIGHT, 5)
        self.assertEqual(
            limits.HEAVY_TOOLS,
            {"get_my_briefing", "get_timeline", "search_workspace", "get_item_line"},
        )
        self.assertEqual(limits.weight_of("get_timeline"), 5)
        self.assertEqual(limits.weight_of("get_task"), 1)
        self.assertEqual(limits.weight_of(None), 1)
        self.assertEqual(limits.weight_of(["get_timeline"]), 1)

    def test_every_tier_carries_every_key_and_all_of_them_are_dark(self):
        table = settings.SEARCH_ENGINE["TIER_QUOTAS"]
        self.assertEqual(set(table), set(limits.MCP_TIER_LIMITS_PROPOSED))
        for tier, row in table.items():
            for key in limits.CAPABILITY_KEYS:
                self.assertIn(key, row, f"{tier} lacks {key}")
                self.assertIsNone(row[key], f"{tier}.{key} must ship None (unlimited)")
        for tier, row in limits.MCP_TIER_LIMITS_PROPOSED.items():
            self.assertEqual(set(row), set(limits.CAPABILITY_KEYS), tier)

    def test_the_docs_table_is_generated_from_the_proposed_numbers(self):
        self.assertEqual(
            limits.proposed_table_markdown(),
            "\n".join(
                [
                    "| Limit | Core | Pro | Max | Enterprise |",
                    "| --- | --- | --- | --- | --- |",
                    "| MCP calls per minute, per key or connected app | 60 | 120 | 240 | unlimited |",
                    "| MCP calls per day, per person across all keys and apps | 3,000 | 10,000 | 30,000 | unlimited |",
                    "| `search_workspace` calls per day | 100 | 300 | 1,000 | unlimited |",
                    "| API keys per person (new ones only) | 3 | 10 | 25 | unlimited |",
                    "| Connected AI apps per person (new ones only) | 3 | 10 | 25 | unlimited |",
                    "| `update_tasks_bulk` items per call | 10 | 30 | 30 | unlimited |",
                    "| `create_task_plan` tasks per call | 10 | 20 | 20 | unlimited |",
                    "| Largest list page a caller may ask for | 50 | 100 | 100 | unlimited |",
                    "| Chat history MCP reads can reach | follows plan | follows plan | follows plan | unlimited |",
                ]
            ),
        )

    def test_the_mode_switch(self):
        with _env():
            self.assertEqual(limits.mode(), "off")
        for raw, want in [
            ("shadow", "shadow"),
            (" SHADOW ", "shadow"),
            ("enforce", "enforce"),
            ("Enforce", "enforce"),
            ("on", "off"),
            ("1", "off"),
            ("", "off"),
            ("shadow;enforce", "off"),
        ]:
            with _env(raw):
                self.assertEqual(limits.mode(), want, raw)

    def test_the_numbers_a_flip_copies_are_valid_limits(self):
        for tier, row in limits.MCP_TIER_LIMITS_PROPOSED.items():
            for key, value in row.items():
                self.assertTrue(limits._valid(key, value), (tier, key))  # noqa: SLF001


# ---------------------------------------------------------------------------------------------
# While dark: nothing at all
# ---------------------------------------------------------------------------------------------


def _battery(case):
    """The same calls, in a fixed order; what comes back is what the twin tests compare."""
    out = []
    for method, params in [
        ("initialize", {"protocolVersion": "2025-11-25"}),
        ("ping", None),
        ("tools/list", None),
        ("tools/call", {"name": "get_task", "arguments": {"task_id": "WRD-7"}}),
        ("tools/call", {"name": "get_task", "arguments": {"task_id": "WRD-999"}}),
        ("tools/call", {"name": "get_task", "arguments": {"nope": 1}}),
        ("tools/call", {"name": "list_tasks", "arguments": {"limit": 500}}),
        ("tools/call", {"name": "update_tasks_bulk", "arguments": {"updates": []}}),
        ("tools/call", {"name": "no_such_tool", "arguments": {}}),
        ("tools/call", {"name": ["x"], "arguments": {}}),
        ("tools/call", {"name": "get_timeline", "arguments": {"anchor_type": "x"}}),
        ("prompts/list", None),
    ]:
        res = case._rpc(method, params)
        body = res.content.decode()
        out.append((res.status_code, res.get("Retry-After"), body))
    return out


class TestDarkMeansNothing(LimitsBase):
    mode = None  # the variable is not set at all

    def test_off_is_the_default_and_writes_nothing(self):
        self.assertEqual(limits.mode(), "off")
        self.rig(
            mcp_calls_per_min=1,
            mcp_calls_per_day=1,
            mcp_search_daily=1,
            mcp_bulk_max_tasks=1,
            mcp_list_page_max=1,
            mcp_apply_message_retention=True,
        )
        for _ in range(8):
            res = self._call("get_task", {"task_id": "WRD-7"})
            self.assertEqual(res.status_code, 200)
            self.assertFalse(res.data["result"]["isError"])
        self.assertEqual(self.counters(), [])
        self.assertEqual(limits._local, {})  # noqa: SLF001

    def test_off_asks_nothing_of_the_database_or_the_store(self):
        self.rig(mcp_calls_per_min=1)
        with mock.patch.object(limits, "plan_for", side_effect=AssertionError("looked up")):
            with mock.patch.object(limits, "throttle_cache", side_effect=AssertionError("cache")):
                with mock.patch.object(quota, "get_message_retention_days") as retention:
                    res = self._call("get_task", {"task_id": "WRD-7"})
        self.assertEqual(res.status_code, 200)
        retention.assert_not_called()

    def test_off_and_enforce_with_every_live_value_none_answer_identically(self):
        """The twin: same requests, same bytes, same number of queries, no counter."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def run(mode):
            cache.clear()
            quota.invalidate_effective_tier([str(self.user.id)])
            # warm the tier cache the same way for both runs
            self._rpc("ping")
            with _env(mode):
                with CaptureQueriesContext(connection) as queries:
                    answers = _battery(self)
            return answers, len(queries), self.counters()

        off, off_queries, off_counters = run(None)
        enforce, enforce_queries, enforce_counters = run("enforce")
        self.assertEqual(off, enforce)
        self.assertEqual(off_counters, [])
        self.assertEqual(enforce_counters, [])
        # The only thing enforce may do is resolve the tier (one cached read): never a query per
        # dimension. Equal here because the tier is cached by the warm-up.
        self.assertEqual(off_queries, enforce_queries)

    def test_enforce_with_none_writes_no_counter_and_refuses_nothing(self):
        for _ in range(30):
            res = self._call("get_task", {"task_id": "WRD-7"})
            self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.counters(), [])


class TestDarkMeansNothingTwo(LimitsBase):
    mode = "enforce"

    def test_enforce_with_the_table_as_shipped_changes_no_argument(self):
        spy = self.stub()
        for name, args in [
            ("list_tasks", {"limit": 500}),
            ("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 99}),
            ("create_task_plan", {"tasks": [{"title": "x"}] * 99}),
            ("list_my_mentions", {"since": "2000-01-01"}),
            ("get_timeline", {"anchor_type": "thread", "anchor_id": "x"}),
            ("list_projects", {}),
        ]:
            self._call(name, args)
            self.assertEqual(spy.call_args.args[1], args, name)


# ---------------------------------------------------------------------------------------------
# Transport limits
# ---------------------------------------------------------------------------------------------


class TestPerMinute(LimitsBase):
    def test_the_call_over_the_limit_is_a_429_in_json_rpc_with_retry_after(self):
        self.stub()
        self.rig(mcp_calls_per_min=3)
        for _ in range(3):
            self.assertEqual(self.call_ok(rid=7).status_code, 200)
        res = self.call_ok(rid=42)
        self.assertEqual(res.status_code, 429)
        wait = int(res["Retry-After"])
        self.assertTrue(1 <= wait <= 60)
        body = res.json()
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["error"]["code"], -32600)
        self.assertEqual(
            body["error"]["data"],
            {
                "limit_reached": True,
                "used": 3,
                "limit": 3,
                "category": "mcp_per_minute",
                "retry_after": wait,
            },
        )
        message = body["error"]["message"]
        self.assertIn("3 per minute", message)
        self.assertIn(f"Wait {wait} second", message)
        self.assertIn("count as 5", message)

    def test_a_refused_call_is_given_back_so_used_does_not_grow(self):
        self.stub()
        self.rig(mcp_calls_per_min=2)
        self.call_ok()
        self.call_ok()
        used = {self.call_ok().json()["error"]["data"]["used"] for _ in range(4)}
        self.assertEqual(used, {2})

    def test_heavy_calls_weigh_five_against_the_minute(self):
        spy = self.stub()
        self.rig(mcp_calls_per_min=6)
        first = self._call("get_timeline", {"anchor_type": "task", "anchor_id": "WRD-7"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.call_ok().status_code, 200)  # 5 + 1 = 6
        over = self.call_ok()
        self.assertEqual(over.status_code, 429)
        self.assertEqual(over.json()["error"]["data"]["used"], 6)
        self.assertEqual(spy.call_count, 2)

    def test_one_heavy_call_is_refused_alone_when_the_limit_is_under_its_weight(self):
        self.stub()
        self.rig(mcp_calls_per_min=4)
        for heavy in ("get_my_briefing", "get_timeline", "search_workspace", "get_item_line"):
            res = self._call(heavy, {})
            self.assertEqual(res.status_code, 429, heavy)
        self.assertEqual(self.call_ok().status_code, 200)

    def test_it_is_per_credential_not_per_person(self):
        self.stub()
        self.rig(mcp_calls_per_min=2)
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {self.raw}")
        self.call_ok()
        self.call_ok()
        self.assertEqual(self.call_ok().status_code, 429)
        self._key(scope=SCOPE_READ)  # a second key of the same person
        self.assertEqual(self.call_ok().status_code, 200)

    def test_the_next_minute_starts_clean(self):
        self.stub()
        self.rig(mcp_calls_per_min=1)
        with mock.patch.object(limits, "_now", return_value=1_800_000_030.0):
            self.call_ok()
            self.assertEqual(self.call_ok().status_code, 429)
        with mock.patch.object(limits, "_now", return_value=1_800_000_095.0):
            self.assertEqual(self.call_ok().status_code, 200)

    def test_only_tools_call_is_counted(self):
        self.rig(mcp_calls_per_min=1)
        for method in ("ping", "tools/list", "prompts/list", "initialize"):
            for _ in range(3):
                self.assertEqual(self._rpc(method).status_code, 200, method)
        self.assertEqual(self.counters(), [])

    def test_a_limit_of_zero_refuses_every_call(self):
        self.rig(mcp_calls_per_min=0)
        self.assertEqual(self.call_ok().status_code, 429)

    def test_it_works_in_the_modern_era_too(self):
        self.stub()
        self.rig(mcp_calls_per_min=1)
        self.call_ok(era="2026-07-28")
        res = self.call_ok(era="2026-07-28")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error"]["data"]["category"], "mcp_per_minute")


class TestPerDay(LimitsBase):
    def test_the_daily_total_is_per_person_across_every_key(self):
        self.stub()
        self.rig(mcp_calls_per_day=3)
        keys = [self.raw, self._key(scope=SCOPE_READ), self._key(scope=SCOPE_WRITE)]
        for raw in keys:
            self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
            self.assertEqual(self.call_ok().status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {keys[0]}")
        res = self.call_ok(rid=5)
        self.assertEqual(res.status_code, 429)
        data = res.json()["error"]["data"]
        self.assertEqual(
            {k: data[k] for k in ("limit_reached", "used", "limit", "category")},
            {"limit_reached": True, "used": 3, "limit": 3, "category": "mcp_per_day"},
        )
        wait = int(res["Retry-After"])
        self.assertTrue(1 <= wait <= 86_400)
        self.assertEqual(data["retry_after"], wait)
        self.assertIn("00:00 UTC", res.json()["error"]["message"])
        self.assertIn("3 calls a day", res.json()["error"]["message"])

    def test_heavy_calls_weigh_five_against_the_day(self):
        self.stub()
        self.rig(mcp_calls_per_day=7)
        self._call("get_my_briefing", {})
        self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.call_ok().status_code, 200)
        over = self._call("get_item_line", {})
        self.assertEqual(over.status_code, 429)
        self.assertEqual(over.json()["error"]["data"]["used"], 7)

    def test_one_person_cannot_use_up_another_persons_day(self):
        self.stub()
        self.rig(mcp_calls_per_day=2)
        self.set_tier(self.user2, "core")
        self.call_ok()
        self.call_ok()
        self.assertEqual(self.call_ok().status_code, 429)
        self._key(scope=SCOPE_WRITE, user=self.user2)
        self.assertEqual(self.call_ok().status_code, 200)

    def test_tomorrow_starts_clean(self):
        self.stub()
        self.rig(mcp_calls_per_day=1)
        with mock.patch.object(limits, "_now", return_value=1_800_000_000.0):
            self.call_ok()
            self.assertEqual(self.call_ok().status_code, 429)
        with mock.patch.object(limits, "_now", return_value=1_800_000_000.0 + 86_400):
            self.assertEqual(self.call_ok().status_code, 200)

    def test_the_minute_limit_is_checked_first_and_refused_calls_return_the_day(self):
        self.stub()
        self.rig(mcp_calls_per_min=1, mcp_calls_per_day=10)
        self.call_ok()
        for _ in range(3):
            self.assertEqual(self.call_ok().status_code, 429)
        # Refused calls were given back: the day saw ONE call.
        self.assertEqual(self.counter(":day:"), 1)


class TestSearchPerDay(LimitsBase):
    def test_search_workspace_has_its_own_daily_limit(self):
        spy = self.stub()
        self.rig(mcp_search_daily=2)
        for _ in range(2):
            self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 200)
        res = self._call("search_workspace", {"query": "x"}, rid=9)
        self.assertEqual(res.status_code, 429)
        data = res.json()["error"]["data"]
        self.assertEqual(
            {k: data[k] for k in ("limit_reached", "used", "limit", "category")},
            {"limit_reached": True, "used": 2, "limit": 2, "category": "mcp_search_daily"},
        )
        self.assertIn("Other tools still work", res.json()["error"]["message"])
        # Other tools are not stopped by it.
        self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(spy.call_count, 3)

    def test_the_search_total_is_per_person_across_every_key(self):
        self.stub()
        self.rig(mcp_search_daily=2)
        for raw in (self.raw, self._key(scope=SCOPE_READ)):
            self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
            self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 200)
        self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 429)

    def test_search_counts_one_per_call_for_the_search_limit_not_five(self):
        self.stub()
        self.rig(mcp_search_daily=5)
        for _ in range(5):
            self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 200)
        self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 429)

    def test_a_search_refused_on_the_minute_is_not_counted_as_a_search(self):
        self.stub()
        self.rig(mcp_calls_per_min=5, mcp_search_daily=2)
        self._call("search_workspace", {"query": "x"})  # weight 5: fills the minute
        self.assertEqual(self._call("search_workspace", {"query": "x"}).status_code, 429)
        self.assertEqual(self.counter(":srch:"), 1)


class TestAFreeCredentialStillGetsItsExistingRefusal(LimitsBase):
    tier = "free"

    def test_free_is_403_by_the_plan_gate_before_any_limit_and_nothing_is_counted(self):
        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table["free"]["mcp_enabled"] = False
        table["free"]["mcp_calls_per_min"] = 0
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table}):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = self.call_ok()
        self.assertEqual(res.status_code, 403)
        self.assertIn("MCP is available on the Core plan and above", res.json()["error"]["message"])
        self.assertEqual(self.counters(), [])


class TestEnterpriseIsUnlimited(LimitsBase):
    tier = "enterprise"

    def test_enterprise_is_never_limited_even_in_shadow(self):
        self.stub()
        for mode in ("enforce", "shadow"):
            with _env(mode):
                for _ in range(15):
                    self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.counters(), [])


# ---------------------------------------------------------------------------------------------
# Shadow: watches, logs, changes nothing
# ---------------------------------------------------------------------------------------------


class TestShadow(LimitsBase):
    mode = limits.SHADOW

    def proposed(self, **values):
        table = {t: dict(row) for t, row in limits.MCP_TIER_LIMITS_PROPOSED.items()}
        table[self.tier].update(values)
        patcher = mock.patch.object(limits, "MCP_TIER_LIMITS_PROPOSED", table)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shadow_answers_exactly_like_off_over_the_proposed_limits(self):
        self.proposed(mcp_calls_per_min=2, mcp_calls_per_day=3)
        spy = self.stub()
        for _ in range(8):
            res = self.call_ok()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data["result"], OK)
        self.assertEqual(spy.call_count, 8)

    def test_shadow_logs_what_would_have_been_refused_with_names_and_numbers_only(self):
        self.proposed(mcp_calls_per_min=2, mcp_calls_per_day=3)
        self.stub()
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            for _ in range(4):
                self.call_ok("get_task", {"task_id": "SECRET-TITLE-999"})
        lines = [r.getMessage() for r in logs.records]
        self.assertIn(
            "mcp.tier_would_limit tier=core category=mcp_per_minute limit=2 used=2 tool=get_task",
            lines,
        )
        self.assertIn(
            "mcp.tier_would_limit tier=core category=mcp_per_day limit=3 used=3 tool=get_task",
            lines,
        )
        text = "\n".join(lines)
        for secret in ("SECRET-TITLE-999", str(self.user.id), str(self.team.team_id), self.raw):
            self.assertNotIn(secret, text)

    def test_shadow_logs_once_per_bucket_and_window_not_once_per_call(self):
        self.proposed(mcp_calls_per_min=1)
        self.stub()
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            for _ in range(10):
                self.call_ok()
        self.assertEqual(
            len([r for r in logs.records if "category=mcp_per_minute" in r.getMessage()]), 1
        )

    def test_shadow_uses_its_own_counters_so_enforce_starts_clean(self):
        self.proposed(mcp_calls_per_min=1)
        self.stub()
        for _ in range(3):
            self.call_ok()
        self.assertTrue(self.counters())
        written = [k for k in self.counters() if ":logged:" not in k]
        self.assertTrue(written)
        self.assertTrue(all(":s:" in k for k in written), written)
        with _env("enforce"):
            self.rig(mcp_calls_per_min=1)
            self.assertEqual(self.call_ok().status_code, 200)

    def test_shadow_never_reads_the_live_values(self):
        # Live says 1 per minute; shadow measures against the proposed 60, so it logs nothing.
        self.rig(mcp_calls_per_min=1)
        self.stub()
        with self.assertNoLogs("origin.services.mcp_tier_limits", "INFO"):
            for _ in range(3):
                self.call_ok()

    def test_shadow_weighs_heavy_calls(self):
        self.proposed(mcp_calls_per_min=6)
        self.stub()
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            self._call("get_timeline", {})
            self.call_ok()
            self.call_ok()
        self.assertTrue(any("used=6" in r.getMessage() for r in logs.records))

    def test_shadow_tool_limits_log_and_change_nothing(self):
        self.proposed(mcp_bulk_max_tasks=2, mcp_list_page_max=5)
        spy = self.stub()
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 4})
            self._call("list_tasks", {"limit": 9})
        lines = [r.getMessage() for r in logs.records]
        self.assertIn(
            "mcp.tier_would_limit tier=core category=mcp_bulk_tasks limit=2 used=4 "
            "tool=update_tasks_bulk",
            lines,
        )
        self.assertIn(
            "mcp.tier_would_limit tier=core category=mcp_list_page limit=5 used=9 tool=list_tasks",
            lines,
        )
        self.assertEqual(spy.call_args_list[0].args[1], {"updates": [{"task_id": "WRD-7"}] * 4})
        self.assertEqual(spy.call_args_list[1].args[1], {"limit": 9})

    def test_shadow_with_the_store_down_skips_the_measurement(self):
        self.proposed(mcp_calls_per_min=1)
        self.stub()
        with mock.patch.object(
            limits, "throttle_cache", side_effect=ConnectionInterrupted(connection=None)
        ):
            with self.assertNoLogs("origin.services.mcp_tier_limits", "INFO"):
                for _ in range(3):
                    self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(limits._local, {})  # noqa: SLF001


# ---------------------------------------------------------------------------------------------
# Tool limits
# ---------------------------------------------------------------------------------------------


class TestBulkSize(LimitsBase):
    def test_update_tasks_bulk_over_the_plan_is_an_error_result_with_a_sentence(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=2)
        res = self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 3})
        result = self._result(res)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"],
            "`update_tasks_bulk` takes at most 2 updates per call on your plan, and this call "
            "has 3. Nothing was changed. Send 2 now and the rest in another call.",
        )
        self.assertEqual(
            result["_meta"],
            {
                "genos/limit": {
                    "limit_reached": True,
                    "category": "mcp_bulk_tasks",
                    "used": 3,
                    "limit": 2,
                }
            },
        )
        spy.assert_not_called()

    def test_at_the_limit_it_runs(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=2)
        self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 2})
        spy.assert_called_once()

    def test_create_task_plan_has_its_own_limit(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=1, mcp_bulk_max_plan_tasks=3)
        refused = self._result(self._call("create_task_plan", {"tasks": [{"title": "a"}] * 4}))
        self.assertTrue(refused["isError"])
        self.assertIn(
            "`create_task_plan` takes at most 3 tasks per call", refused["content"][0]["text"]
        )
        self.assertEqual(refused["_meta"]["genos/limit"]["category"], "mcp_bulk_plan_tasks")
        spy.assert_not_called()
        self._call("create_task_plan", {"tasks": [{"title": "a"}] * 3})
        spy.assert_called_once()

    def test_what_a_tool_refuses_first_is_still_refused_first(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=1)
        # A read-only key: the read-only refusal comes from the tool, not a plan sentence.
        self._key(scope=SCOPE_READ)
        spy.reset_mock()
        res = self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 3})
        self.assertNotIn("_meta", self._result(res))
        spy.assert_called_once()  # reached tools.call, which gives the read-only answer
        # An argument it does not take.
        spy.reset_mock()
        self._key(scope=SCOPE_WRITE)
        self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 3, "bogus": 1})
        spy.assert_called_once()

    def test_a_list_that_is_not_a_list_is_left_to_the_tool(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=1)
        self._call("update_tasks_bulk", {"updates": "x"})
        spy.assert_called_once()

    def test_the_rest_of_the_tool_list_is_unchanged_by_the_limits(self):
        """Which tools a plan gets is not this phase's: the listing is identical."""
        before = [t["name"] for t in self._result(self._rpc("tools/list"))["tools"]]
        self.rig(
            mcp_calls_per_min=1,
            mcp_bulk_max_tasks=1,
            mcp_bulk_max_plan_tasks=1,
            mcp_list_page_max=1,
        )
        after = [t["name"] for t in self._result(self._rpc("tools/list"))["tools"]]
        self.assertEqual(before, after)
        self.assertIn("update_tasks_bulk", after)
        self.assertIn("create_task_plan", after)


class TestPageSize(LimitsBase):
    def test_a_page_over_the_plan_is_refused_in_words_and_at_the_limit_it_runs(self):
        spy = self.stub()
        self.rig(mcp_list_page_max=50)
        refused = self._result(self._call("list_milestones", {"limit": 51}))
        self.assertTrue(refused["isError"])
        self.assertEqual(
            refused["content"][0]["text"],
            "`list_milestones` returns at most 50 items per page on your plan, and `limit` was "
            "51. Ask again with `limit` 50 or less, and page on for the rest.",
        )
        self.assertEqual(
            refused["_meta"]["genos/limit"],
            {"limit_reached": True, "category": "mcp_list_page", "used": 51, "limit": 50},
        )
        spy.assert_not_called()
        self._call("list_milestones", {"limit": 50})
        spy.assert_called_once()

    def test_calendar_max_results_is_a_page_size_too(self):
        spy = self.stub()
        self.rig(mcp_list_page_max=10)
        refused = self._result(self._call("list_calendar_events", {"max_results": 11}))
        self.assertTrue(refused["isError"])
        self.assertIn("`max_results` was 11", refused["content"][0]["text"])
        spy.assert_not_called()

    def test_a_non_integer_limit_is_left_to_the_tool(self):
        spy = self.stub()
        self.rig(mcp_list_page_max=5)
        self._call("list_tasks", {"limit": "many"})
        self._call("list_tasks", {"limit": True})
        self.assertEqual(spy.call_count, 2)

    def test_a_list_that_would_default_above_the_plan_is_given_the_plans_page(self):
        spy = self.stub()
        self.rig(mcp_list_page_max=50)
        real = tools.input_schema

        def with_limit(tool):
            schema = real(tool)
            return {**schema, "properties": {**schema.get("properties", {}), "limit": {}}}

        with mock.patch.object(tools, "input_schema", side_effect=with_limit):
            self._call("list_projects", {})
            self.assertEqual(spy.call_args.args[1], {"limit": 50})
            self._call("list_projects", {"limit": 20})
            self.assertEqual(spy.call_args.args[1], {"limit": 20})
        # Not while the tool declares no `limit` (an argument it does not take is refused).
        self._call("list_projects", {})
        self.assertEqual(spy.call_args.args[1], {})

    def test_shadow_never_gives_a_list_a_page_size(self):
        spy = self.stub()
        real = tools.input_schema

        def with_limit(tool):
            schema = real(tool)
            return {**schema, "properties": {**schema.get("properties", {}), "limit": {}}}

        self.rig(mcp_list_page_max=10)
        with _env("shadow"), mock.patch.object(tools, "input_schema", side_effect=with_limit):
            self._call("list_projects", {})
        self.assertEqual(spy.call_args.args[1], {})

    def test_a_boolean_is_not_a_page_size(self):
        spy = self.stub()
        self.rig(mcp_list_page_max=0)
        self._call("list_tasks", {"limit": True})
        spy.assert_called_once()
        self.assertTrue(self._result(self._call("list_tasks", {"limit": 1}))["isError"])

    def test_the_plan_with_a_page_of_100_or_none_is_never_given_one(self):
        spy = self.stub()
        real = tools.input_schema

        def with_limit(tool):
            schema = real(tool)
            return {**schema, "properties": {**schema.get("properties", {}), "limit": {}}}

        with mock.patch.object(tools, "input_schema", side_effect=with_limit):
            for cap in (100, None):
                self.rig(mcp_list_page_max=cap)
                self._call("list_projects", {})
                self.assertEqual(spy.call_args.args[1], {}, cap)


# ---------------------------------------------------------------------------------------------
# The failure policy
# ---------------------------------------------------------------------------------------------


class TestACallNothingLimitsCostsNothing(LimitsBase):
    def test_a_plain_read_gets_no_limit_evaluation_at_all_in_prepare(self):
        self.rig(mcp_list_page_max=5, mcp_bulk_max_tasks=1)
        ctx = mock.Mock(user_id=str(self.user.id), team_id=str(self.team.team_id))
        args = {"task_id": "WRD-7"}
        with mock.patch.object(limit_gate, "_tool_would_refuse_first") as first:
            with mock.patch.object(limits, "plan_for") as plan:
                result = limit_gate.prepare("get_task", args, ctx, SCOPE_WRITE, False)
        self.assertEqual(result, (args, None))
        self.assertIs(result[0], args)
        first.assert_not_called()
        plan.assert_not_called()


class TestFailurePolicy(LimitsBase):
    tier = "pro"

    def rig_core_and_pro(self, core, pro):
        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table["core"].update(core)
        table["pro"].update(pro)
        ctx = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table})
        ctx.enable()
        self.addCleanup(ctx.disable)
        quota.invalidate_effective_tier([str(self.user.id)])

    def test_a_tier_lookup_that_fails_uses_cores_row_never_unlimited(self):
        self.stub()
        self.rig_core_and_pro({"mcp_calls_per_min": 2}, {"mcp_calls_per_min": None})
        # The person is Pro (unlimited here)...
        for _ in range(4):
            self.assertEqual(self.call_ok().status_code, 200)
        cache.clear()
        # ...but when the tier cannot be resolved the call is held to Core's row.
        with mock.patch.object(quota, "get_effective_tier", side_effect=RuntimeError("db")):
            self.assertEqual(self.call_ok().status_code, 200)
            self.assertEqual(self.call_ok().status_code, 200)
            self.assertEqual(self.call_ok().status_code, 429)

    def test_the_plan_gate_itself_is_not_touched_by_a_lookup_failure_in_the_limits(self):
        with mock.patch.object(quota, "get_effective_tier", side_effect=RuntimeError("db")):
            plan = limits.plan_for(str(self.user.id), limits.ENFORCE)
        self.assertEqual(plan.tier, "core")

    def test_a_missing_key_is_unlimited_like_every_other_capability(self):
        self.stub()
        self.rig(mcp_calls_per_min=_MISSING, mcp_calls_per_day=_MISSING)
        for _ in range(6):
            self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.counters(), [])

    def test_a_value_that_cannot_be_a_limit_is_cores_value_for_the_key(self):
        self.stub()
        self.rig_core_and_pro({"mcp_calls_per_min": 2}, {"mcp_calls_per_min": "lots"})
        with self.assertLogs("origin.services.mcp_tier_limits", "WARNING"):
            self.call_ok()
        self.call_ok()
        self.assertEqual(self.call_ok().status_code, 429)
        for bad in (-1, True, 2.5, [1]):
            self.assertFalse(limits._valid("mcp_calls_per_min", bad), bad)  # noqa: SLF001

    def test_a_broken_value_and_a_broken_core_is_none_not_a_crash(self):
        self.stub()
        self.rig_core_and_pro({"mcp_calls_per_min": "x"}, {"mcp_calls_per_min": "y"})
        self.assertEqual(self.call_ok().status_code, 200)

    def test_with_the_store_down_the_minute_counts_in_this_process_against_the_plans_number(self):
        self.stub()
        self.rig(mcp_calls_per_min=2)
        with mock.patch.object(
            limits, "throttle_cache", side_effect=ConnectionInterrupted(connection=None)
        ):
            self.assertEqual(self.call_ok().status_code, 200)
            self.assertEqual(self.call_ok().status_code, 200)
            self.assertEqual(self.call_ok().status_code, 429)

    def test_with_the_store_down_the_totals_fall_back_to_cores_numbers(self):
        self.stub()
        self.rig_core_and_pro(
            {"mcp_calls_per_day": 2, "mcp_search_daily": 1},
            {"mcp_calls_per_day": 1000, "mcp_search_daily": 1000},
        )
        with mock.patch.object(
            limits, "throttle_cache", side_effect=ConnectionInterrupted(connection=None)
        ):
            self.assertEqual(self.call_ok().status_code, 200)
            self.assertEqual(self.call_ok().status_code, 200)
            third = self.call_ok()
            self.assertEqual(third.status_code, 429)
            self.assertEqual(third.json()["error"]["data"]["limit"], 2)
            self.assertEqual(third.json()["error"]["data"]["category"], "mcp_per_day")

    def test_with_the_store_up_the_plan_own_total_applies(self):
        self.stub()
        self.rig_core_and_pro({"mcp_calls_per_day": 2}, {"mcp_calls_per_day": 1000})
        for _ in range(5):
            self.assertEqual(self.call_ok().status_code, 200)

    def test_a_limit_that_crashes_never_fails_the_call(self):
        self.stub()
        self.rig(mcp_calls_per_min=1)
        with mock.patch.object(limits, "admit_call", side_effect=RuntimeError("boom")):
            for _ in range(3):
                self.assertEqual(self.call_ok().status_code, 200)
        # Counted normally again: the first call passes, the second is the minute's refusal.
        self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.call_ok().status_code, 429)

    def test_a_crash_in_the_tool_limits_runs_the_call_unchanged(self):
        spy = self.stub()
        self.rig(mcp_bulk_max_tasks=1)
        with mock.patch.object(limit_gate, "_bulk_refusal", side_effect=RuntimeError("boom")):
            self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 3})
        spy.assert_called_once()


class TestNoOracleAndNoPersonalLogs(LimitsBase):
    def test_a_refusal_names_only_numbers_and_the_category(self):
        self.stub()
        self.rig(mcp_calls_per_min=1)
        self.call_ok()
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            res = self.call_ok("get_task", {"task_id": "VERY-SECRET-1"})
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["mcp.tier_limited tier=core category=mcp_per_minute limit=1 used=1 tool=get_task"],
        )
        payload = json.dumps(res.json())
        self.assertNotIn("VERY-SECRET-1", payload)
        self.assertNotIn(str(self.user.id), payload)

    def test_a_tool_name_a_client_made_up_is_not_logged(self):
        self.stub()
        self.rig(mcp_calls_per_min=1)
        self._call("x" * 200, {})
        with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
            self._call("x y\nforged line", {})
        self.assertNotIn("forged", logs.output[0])
        self.assertTrue(logs.output[0].endswith("tool=-"))


# ---------------------------------------------------------------------------------------------
# Chat history (the app's own window, over MCP)
# ---------------------------------------------------------------------------------------------


class TestHistoryOnGetChatThread(McpChatBase):
    mode = limits.ENFORCE

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        env = _env(self.mode)
        env.start()
        self.addCleanup(env.stop)
        self.user.tier = "core"
        self.user.save()
        old = timezone.now() - timedelta(days=400)
        self.old = self.say(self.gm, "an old message")
        Message.objects.filter(pk=self.old.pk).update(ts_sent_at=old)
        self.new = self.say(self.gm, "a recent message")

    def rig(self, **values):
        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table["core"].update(values)
        ctx = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table})
        ctx.enable()
        self.addCleanup(ctx.disable)
        quota.invalidate_effective_tier([str(self.user.id)])

    def read(self, **extra):
        return self.call(
            "get_chat_thread", {"chat_type": "GM", "chat_id": str(self.gm.id), **extra}
        )[0]["structuredContent"]

    def texts(self, payload):
        return [m["text"] for m in payload["messages"]]

    def test_with_the_live_key_none_the_old_message_is_still_there(self):
        payload = self.read()
        self.assertEqual(len(payload["messages"]), 2)

    def test_when_the_key_is_flipped_core_cannot_read_past_its_365_days(self):
        self.rig(mcp_apply_message_retention=True)
        payload = self.read()
        self.assertEqual(len(payload["messages"]), 1)
        self.assertIn("a recent message", payload["messages"][0]["text"])
        self.assertFalse(payload["has_more"])
        self.assertNotIn("next_cursor", payload)

    def test_the_days_are_the_apps_own_function_not_a_number_of_ours(self):
        self.rig(mcp_apply_message_retention=True, message_retention_days=500)
        self.assertEqual(len(self.read()["messages"]), 2)
        self.rig(mcp_apply_message_retention=True, message_retention_days=30)
        self.assertEqual(len(self.read()["messages"]), 1)

    def test_pro_and_up_have_no_window_as_in_the_app(self):
        self.user.tier = "pro"
        self.user.save()
        table = {t: dict(c) for t, c in settings.SEARCH_ENGINE["TIER_QUOTAS"].items()}
        table["pro"]["mcp_apply_message_retention"] = True
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "TIER_QUOTAS": table}):
            quota.invalidate_effective_tier([str(self.user.id)])
            self.assertEqual(len(self.read()["messages"]), 2)

    def test_hiding_the_oldest_page_ends_the_paging_instead_of_walking_into_hidden_history(self):
        for i in range(5):
            m = self.say(self.gm, f"older {i}")
            Message.objects.filter(pk=m.pk).update(ts_sent_at=timezone.now() - timedelta(days=401))
        self.rig(mcp_apply_message_retention=True)
        payload = self.read(limit=3)
        self.assertFalse(payload["has_more"])
        self.assertNotIn("next_cursor", payload)

    def test_the_summary_says_what_the_window_is(self):
        self.rig(mcp_apply_message_retention=True)
        text = self.call("get_chat_thread", {"chat_type": "GM", "chat_id": str(self.gm.id)})[1]
        self.assertIn("history window is the last 365 days", text)

    def test_when_nothing_is_left_it_says_so(self):
        Message.objects.filter(pk=self.new.pk).update(
            ts_sent_at=timezone.now() - timedelta(days=500)
        )
        self.rig(mcp_apply_message_retention=True)
        result, text = self.call("get_chat_thread", {"chat_type": "GM", "chat_id": str(self.gm.id)})
        self.assertEqual(result["structuredContent"]["messages"], [])
        self.assertIn("hidden by your plan's chat history window", text)

    def test_a_thread_keeps_its_has_more_which_is_about_newer_replies(self):
        replies_root = self.say(self.gm, "root")
        for _ in range(3):
            self.say(self.gm, "reply", reply_to=replies_root)
        self.rig(mcp_apply_message_retention=True)
        payload = self.read(thread_id=str(replies_root.id))
        self.assertEqual(len(payload["messages"]), 4)

    def test_off_with_the_live_key_flipped_still_hides_nothing(self):
        self.rig(mcp_apply_message_retention=True)
        with _env(None):
            self.assertEqual(len(self.read()["messages"]), 2)
            self.assertIsNone(limits.history_window(str(self.user.id)))

    def test_a_message_with_no_readable_time_is_kept_and_a_threads_has_more_is_kept(self):
        from origin.views.public.mcp.history_window import hide_old_messages

        self.rig(mcp_apply_message_retention=True)
        old = {"ts": "2000-01-01T00:00:00+00:00", "text": "old"}
        undated = {"text": "no time"}
        payload = {
            "messages": [old, undated, {"ts": "not a time", "text": "odd"}],
            "has_more": True,
            "thread_id": "t1",
        }
        out = hide_old_messages(payload, {}, {}, mock.Mock(user_id=str(self.user.id)))
        self.assertEqual([m["text"] for m in out["messages"]], ["no time", "odd"])
        self.assertTrue(out["has_more"])

    def test_off_returns_the_payload_it_was_given(self):
        from origin.views.public.mcp.history_window import hide_old_messages

        payload = {"messages": [{"ts": "2000-01-01T00:00:00+00:00"}]}
        with _env(None):
            self.assertIs(hide_old_messages(payload, {}, {}, mock.Mock(user_id="x")), payload)
        with _env("enforce"):  # live key None: same object back
            self.assertIs(
                hide_old_messages(payload, {}, {}, mock.Mock(user_id=str(self.user.id))), payload
            )

    def test_shadow_logs_how_many_would_be_hidden_and_hides_nothing(self):
        with _env("shadow"):
            with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
                payload = self.read()
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            [
                "mcp.tier_would_limit tier=core category=mcp_history limit=365 used=1 "
                "tool=get_chat_thread"
            ],
        )


class TestHistoryByArgument(LimitsBase):
    def test_mentions_start_no_earlier_than_the_window_when_flipped(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        self._call("list_my_mentions", {})
        since = spy.call_args.args[1]["since"]
        cutoff = timezone.now() - timedelta(days=365)
        self.assertLess(abs((timezone.datetime.fromisoformat(since) - cutoff).total_seconds()), 60)
        self._call("list_my_mentions", {"since": "2001-01-01"})
        self.assertNotEqual(spy.call_args.args[1]["since"], "2001-01-01")

    def test_a_since_inside_the_window_or_one_the_tool_cannot_read_is_left_alone(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        recent = (timezone.now() - timedelta(days=3)).isoformat()
        self._call("list_my_mentions", {"since": recent})
        self.assertEqual(spy.call_args.args[1], {"since": recent})
        self._call("list_my_mentions", {"since": "not a date"})
        self.assertEqual(spy.call_args.args[1], {"since": "not a date"})

    def test_a_thread_timeline_is_told_from_the_window_and_a_task_timeline_is_not(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        self._call("get_timeline", {"anchor_type": "thread", "anchor_id": "x"})
        since = spy.call_args.args[1]["since"]
        self.assertTrue(
            since.endswith("T00:00:00+00:00"), since
        )  # one cutoff per day: cursors hold
        self._call("get_timeline", {"anchor_type": "task", "anchor_id": "WRD-7"})
        self.assertEqual(spy.call_args.args[1], {"anchor_type": "task", "anchor_id": "WRD-7"})

    def test_a_task_timeline_is_never_windowed_whatever_its_dates(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        old = (timezone.now() - timedelta(days=900)).isoformat()
        args = {"anchor_type": "task", "anchor_id": "WRD-7", "since": old, "until": old}
        self._call("get_timeline", args)
        self.assertEqual(spy.call_args.args[1], args)

    def test_shadow_changes_no_timeline_argument(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        old = (timezone.now() - timedelta(days=500)).isoformat()
        args = {"anchor_type": "thread", "anchor_id": "x", "since": old}
        with _env("shadow"):
            self._call("get_timeline", args)
            self.assertEqual(spy.call_args.args[1], args)
            args = {"anchor_type": "chat", "anchor_id": "x", "since": old, "until": old}
            self._call("get_timeline", args)
            self.assertEqual(spy.call_args.args[1], args)

    def test_a_chat_window_that_ends_before_the_history_is_refused_in_words(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        old = (timezone.now() - timedelta(days=500)).isoformat()
        result = self._result(
            self._call(
                "get_timeline",
                {"anchor_type": "chat", "anchor_id": "x", "since": old, "until": old},
            )
        )
        self.assertTrue(result["isError"])
        self.assertIn("last 365 days", result["content"][0]["text"])
        self.assertEqual(
            result["_meta"]["genos/limit"],
            {"limit_reached": True, "category": "mcp_history", "limit": 365},
        )
        spy.assert_not_called()

    def test_a_chat_window_that_straddles_the_cutoff_is_clamped(self):
        spy = self.stub()
        self.rig(mcp_apply_message_retention=True)
        now = timezone.now()
        self._call(
            "get_timeline",
            {
                "anchor_type": "chat",
                "anchor_id": "x",
                "since": (now - timedelta(days=380)).isoformat(),
                "until": (now - timedelta(days=360)).isoformat(),
            },
        )
        self.assertTrue(spy.call_args.args[1]["since"].endswith("T00:00:00+00:00"))

    def test_shadow_logs_a_reach_beyond_the_window_and_changes_nothing(self):
        spy = self.stub()
        with _env("shadow"):
            with self.assertLogs("origin.services.mcp_tier_limits", "INFO") as logs:
                self._call("list_my_mentions", {"since": "2001-01-01"})
        self.assertEqual(spy.call_args.args[1], {"since": "2001-01-01"})
        self.assertIn("category=mcp_history limit=365", logs.output[0])

    def test_briefing_and_item_lines_look_back_less_than_any_window_so_need_no_clamp(self):
        from origin.services import briefing

        self.assertLess(briefing.MAX_LOOKBACK, timedelta(days=365))
        self.assertLess(
            briefing.MAX_LOOKBACK,
            timedelta(days=limits.MCP_TIER_LIMITS_PROPOSED and 365),
        )
        # Core's message window is the shortest the plans use (Free's is longer, 180 days).
        core_days = settings.SEARCH_ENGINE["TIER_QUOTAS"]["core"]["message_retention_days"]
        self.assertGreater(timedelta(days=core_days), briefing.MAX_LOOKBACK)


# ---------------------------------------------------------------------------------------------
# One person's refusal never affects another; nothing about the existing REST throttle changes
# ---------------------------------------------------------------------------------------------


class TestExistingThrottleIsUntouched(LimitsBase):
    def test_the_authenticated_429_of_the_public_throttle_is_still_the_plain_detail(self):
        # The per-credential 120/min stays as it is; MCP routes answer with it exactly as before.
        def refuse(throttle, request, view):
            throttle._cache_was_down = False  # noqa: SLF001
            return False

        with mock.patch.object(PublicApiThrottle, "allow_request", refuse):
            with mock.patch.object(PublicApiThrottle, "wait", return_value=30):
                res = self._rpc("ping")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(set(res.json()), {"detail"})


class TestChannelFixtureSanity(McpChatBase):
    def test_the_fixture_is_a_channel_both_users_are_in(self):
        self.assertTrue(Channel.objects.filter(kind=ChannelKind.GM).exists())
        self.assertEqual(ChannelMember.objects.filter(channel=self.gm).count(), 2)

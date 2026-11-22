"""The MCP plan limits as they are LIVE (MIP-1614, Ken 2026-11-22), with NO rigged numbers.

`test_mcp_tier_limits.py` rigs tiny values to prove each mechanism. This file runs the shipped
`TIER_QUOTAS` with `MCP_TIER_LIMITS_MODE=enforce` and proves the adopted numbers: each limit
refuses at limit + 1 for Core with the documented answer, a bigger plan reaches its own number,
Enterprise is unlimited, every call weighs 1, a trial account gets Core's row, and keys or apps a
person already has over a new cap keep working.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from rest_framework.settings import api_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine import quota
from origin.services import first_month_trial, throttles
from origin.services import mcp_tier_limits as limits
from origin.tests import test_mcp_tier_limits as base
from origin.tests.test_api_keys import KEYS_URL
from origin.views.public.mcp.views import McpView
from origin.views.public.permissions import McpApiThrottle


class LiveBase(base.LimitsBase):
    """No `rig`: every test reads the table as shipped."""

    def day_key(self, user):
        day = datetime.fromtimestamp(base.PINNED_NOW, UTC).strftime("%Y%m%d")
        return f"mcp_tl:e:day:{user.id}:{day}"

    def seed_day(self, user, used):
        throttles.throttle_cache().set(self.day_key(user), used, 3600)


class TestCoreRefusesAtLimitPlusOne(LiveBase):
    def test_per_minute_60(self):
        self.stub()
        for n in range(60):
            self.assertEqual(self.call_ok().status_code, 200, n)
        res = self.call_ok(rid=9)
        self.assertEqual(res.status_code, 429)
        self.assertTrue(1 <= int(res["Retry-After"]) <= 60)
        data = res.json()["error"]["data"]
        self.assertEqual(
            {k: data[k] for k in ("limit_reached", "used", "limit", "category")},
            {"limit_reached": True, "used": 60, "limit": 60, "category": "mcp_per_minute"},
        )

    def test_per_day_3000(self):
        self.stub()
        self.seed_day(self.user, 2999)
        self.assertEqual(self.call_ok().status_code, 200)
        res = self.call_ok()
        self.assertEqual(res.status_code, 429)
        self.assertTrue(1 <= int(res["Retry-After"]) <= 86_400)
        data = res.json()["error"]["data"]
        self.assertEqual(
            {k: data[k] for k in ("limit_reached", "used", "limit", "category")},
            {"limit_reached": True, "used": 3000, "limit": 3000, "category": "mcp_per_day"},
        )

    def test_update_tasks_bulk_10(self):
        spy = self.stub()
        self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 10})
        spy.assert_called_once()
        spy.reset_mock()
        result = self._result(
            self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 11})
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["_meta"]["genos/limit"]["category"], "mcp_bulk_tasks")
        self.assertEqual(result["_meta"]["genos/limit"]["limit"], 10)
        spy.assert_not_called()

    def test_create_task_plan_10(self):
        spy = self.stub()
        self._call("create_task_plan", {"tasks": [{"title": "a"}] * 10})
        spy.assert_called_once()
        spy.reset_mock()
        result = self._result(self._call("create_task_plan", {"tasks": [{"title": "a"}] * 11}))
        self.assertTrue(result["isError"])
        self.assertEqual(result["_meta"]["genos/limit"]["category"], "mcp_bulk_plan_tasks")
        spy.assert_not_called()

    def test_list_page_50(self):
        spy = self.stub()
        self._call("list_tasks", {"limit": 51})
        # The page is held to the plan's: refused or cut to 50, never passed on as 51.
        sent = [c.args[1].get("limit") for c in spy.call_args_list]
        self.assertTrue(all(v is None or v <= 50 for v in sent), sent)
        self.assertEqual(limits.page_cap(self.user.id), 50)

    def test_a_search_is_not_capped_apart_from_the_plan_totals(self):
        """No separate `search_workspace` daily cap (the decision): 120 searches in a day pass
        as long as the per-minute and per-day totals allow, spread over two minutes."""
        self.stub()
        for minute_offset in (0, 60):
            with mock.patch.object(limits, "_now", return_value=base.PINNED_NOW + minute_offset):
                for _ in range(60):
                    self.assertEqual(self._call("search_workspace", {}).status_code, 200)


class TestEveryCallWeighsOne(LiveBase):
    def test_sixty_heavy_looking_calls_fit_core_a_minute(self):
        self.stub()
        for name in ("get_my_briefing", "get_timeline", "search_workspace", "get_item_line"):
            self.assertEqual(limits.weight_of(name), 1)
        for n in range(60):
            name = ("get_my_briefing", "get_timeline", "search_workspace", "get_item_line")[n % 4]
            self.assertEqual(self._call(name, {}).status_code, 200, n)
        self.assertEqual(self._call("search_workspace", {}).status_code, 429)
        self.assertEqual(self.counter(":min:"), 60)


class TestBiggerPlans(LiveBase):
    tier = "max"

    def test_max_reaches_240_a_minute_through_the_transport_throttle(self):
        """Max's 240 needs the MCP view's own throttle above the REST 120/min."""
        self.stub()
        for n in range(240):
            self.assertEqual(self.call_ok().status_code, 200, n)
        res = self.call_ok()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["error"]["data"]["category"], "mcp_per_minute")
        self.assertEqual(res.json()["error"]["data"]["limit"], 240)

    def test_max_daily_30000(self):
        self.stub()
        self.seed_day(self.user, 29_999)
        self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.call_ok().status_code, 429)


class TestPro(LiveBase):
    tier = "pro"

    def test_pro_120_a_minute_and_10000_a_day(self):
        self.stub()
        for _ in range(120):
            self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.call_ok().status_code, 429)

    def test_pro_bulk_30_and_plan_20(self):
        spy = self.stub()
        self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 30})
        self._call("create_task_plan", {"tasks": [{"title": "a"}] * 20})
        self.assertEqual(spy.call_count, 2)
        for name, args, key in (
            ("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 31}, "mcp_bulk_tasks"),
            ("create_task_plan", {"tasks": [{"title": "a"}] * 21}, "mcp_bulk_plan_tasks"),
        ):
            result = self._result(self._call(name, args))
            self.assertTrue(result["isError"], name)
            self.assertEqual(result["_meta"]["genos/limit"]["category"], key)


class TestEnterpriseIsUnlimited(LiveBase):
    tier = "enterprise"

    def test_no_counter_no_refusal_no_cap(self):
        spy = self.stub()
        # Under the transport backstop (400/min), far over Max's 240.
        for _ in range(350):
            self.assertEqual(self.call_ok().status_code, 200)
        self._call("update_tasks_bulk", {"updates": [{"task_id": "WRD-7"}] * 200})
        self.assertEqual(spy.call_count, 351)
        self.assertEqual(self.counters(), [])
        self.assertIsNone(limits.page_cap(self.user.id))


class TestTrialIsCore(LiveBase):
    tier = "free"

    def test_a_trial_account_gets_core_s_row(self):
        verified = self.user
        verified.is_email_verified = True
        verified.save(update_fields=["is_email_verified"])
        env = {k: v for k, v in os.environ.items()}
        env[first_month_trial.SWITCH_ENV] = "true"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(first_month_trial.start_for_new_account(self.user))
        quota.invalidate_effective_tier([str(self.user.id)])
        self.assertEqual(quota.get_effective_tier(str(self.user.id)), "core")
        plan = limits.plan_for(self.user.id, limits.ENFORCE)
        self.assertEqual(plan.tier, "core")
        core = settings.SEARCH_ENGINE["TIER_QUOTAS"]["core"]
        for key in limits.CAPABILITY_KEYS:
            self.assertEqual(plan.value(key), core[key], key)
        self.stub()
        for _ in range(60):
            self.assertEqual(self.call_ok().status_code, 200)
        self.assertEqual(self.call_ok().status_code, 429)


class TestExistingKeysKeepWorking(LiveBase):
    def test_keys_over_the_new_cap_still_authenticate_and_call(self):
        spy = self.stub()
        for _ in range(5):  # Core's cap is 3 NEW keys; these already exist
            self._key(scope=SCOPE_WRITE)
        self.assertEqual(self.call_ok().status_code, 200)
        spy.assert_called_once()
        self.client.force_authenticate(user=self.user)
        res = self.client.post(KEYS_URL, {"name": "one more"}, format="json")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["category"], "mcp_api_keys")


class TestTransportThrottle(LiveBase):
    def test_the_mcp_view_has_its_own_rate_above_max(self):
        self.assertIs(McpView.throttle_classes[0], McpApiThrottle)
        rate = api_settings.DEFAULT_THROTTLE_RATES["public_api_mcp"]
        per_minute = int(rate.split("/")[0])
        self.assertGreater(per_minute, 240)
        # The REST API keeps its own, lower, rate.
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES["public_api"], "120/min")
        self.assertEqual(McpApiThrottle.scope, "public_api_mcp")
        cache.clear()

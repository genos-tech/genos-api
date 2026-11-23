"""One `get_my_briefing` in flight per member (MIP-2092, part 2): the gate and the MCP endpoint."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from origin.models.common.api_key_models import (
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.services import briefing_gate
from origin.services.throttles import CacheDown
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import _mcp_enabled_everywhere

MCP = "/api/public/v1/mcp"


class BriefingGateUnitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_first_call_holds_and_second_is_busy(self):
        first = briefing_gate.enter("t1", "u1")
        self.assertIsInstance(first, briefing_gate.Held)
        second = briefing_gate.enter("t1", "u1")
        self.assertIsInstance(second, briefing_gate.Busy)
        self.assertEqual(second.retry_after, briefing_gate.RETRY_AFTER_SECONDS)

    def test_release_lets_the_next_call_through(self):
        first = briefing_gate.enter("t1", "u1")
        briefing_gate.leave(first)
        self.assertIsInstance(briefing_gate.enter("t1", "u1"), briefing_gate.Held)

    def test_another_member_or_team_is_not_blocked(self):
        briefing_gate.enter("t1", "u1")
        self.assertIsInstance(briefing_gate.enter("t1", "u2"), briefing_gate.Held)
        self.assertIsInstance(briefing_gate.enter("t2", "u1"), briefing_gate.Held)

    def test_a_call_only_releases_its_own_lock(self):
        # The first call's lock expired and a second call took the key: the first call finishing
        # late must not free the second's lock.
        stale = briefing_gate.enter("t1", "u1")
        cache.delete(stale.key)
        second = briefing_gate.enter("t1", "u1")
        self.assertIsInstance(second, briefing_gate.Held)
        briefing_gate.leave(stale)
        self.assertIsInstance(briefing_gate.enter("t1", "u1"), briefing_gate.Busy)

    def test_the_lock_has_a_ttl(self):
        with mock.patch("origin.services.briefing_gate.throttle_cache") as store:
            store.return_value.add.return_value = True
            briefing_gate.enter("t1", "u1")
        args = store.return_value.add.call_args.args
        self.assertEqual(args[2], briefing_gate.LOCK_SECONDS)

    def test_cache_down_allows_the_call(self):
        with mock.patch("origin.services.briefing_gate.throttle_cache", side_effect=CacheDown):
            self.assertIsNone(briefing_gate.enter("t1", "u1"))
        with mock.patch(
            "origin.services.briefing_gate.throttle_cache", side_effect=OSError("redis down")
        ):
            self.assertIsNone(briefing_gate.enter("t1", "u1"))

    def test_leave_never_raises_when_the_cache_is_down(self):
        held = briefing_gate.Held("k", "tok")
        with mock.patch("origin.services.briefing_gate.throttle_cache", side_effect=OSError):
            briefing_gate.leave(held)  # no exception
        briefing_gate.leave(None)

    def test_the_busy_result_is_a_suggestion_not_an_error(self):
        result = briefing_gate.busy_result(briefing_gate.Busy(10))
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("already being built", text)
        self.assertIn("Retry `get_my_briefing` in about 10 seconds", text)
        self.assertEqual(result["_meta"]["genos/briefing"], {"in_flight": True, "retry_after": 10})

    def test_the_switch_turns_the_gate_off(self):
        with mock.patch.object(briefing_gate, "_ENABLED", False):
            self.assertIsNone(briefing_gate.enter("t1", "u1"))
            self.assertIsNone(briefing_gate.enter("t1", "u1"))


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class BriefingGateEndpointTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
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

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def _call(self, name="get_my_briefing", arguments=None):
        return self.client.post(
            MCP,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            format="json",
        )

    def test_a_briefing_runs_and_frees_the_lock(self):
        first = self._call()
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.data["result"]["isError"])
        self.assertNotIn("genos/briefing", first.data["result"].get("_meta", {}))
        # Sequential calls are not blocked: the lock was released.
        second = self._call()
        self.assertNotIn("genos/briefing", second.data["result"].get("_meta", {}))

    def test_a_second_briefing_while_one_runs_gets_the_suggestion(self):
        seen = {}

        def during(*_a, **_k):
            # A parallel call of the same member arrives while the first is being built.
            seen["res"] = self._call()
            return {"content": [{"type": "text", "text": "built"}], "isError": False}

        with mock.patch("origin.views.public.mcp.views.tools.call", side_effect=during):
            first = self._call()
        self.assertEqual(first.data["result"]["content"][0]["text"], "built")
        second = seen["res"].data["result"]
        self.assertFalse(second["isError"])
        self.assertIn("already being built", second["content"][0]["text"])
        self.assertTrue(second["_meta"]["genos/briefing"]["in_flight"])
        # The first finished: its lock is gone and the next call runs.
        with mock.patch(
            "origin.views.public.mcp.views.tools.call",
            return_value={"content": [{"type": "text", "text": "again"}], "isError": False},
        ):
            third = self._call()
        self.assertEqual(third.data["result"]["content"][0]["text"], "again")

    def test_the_lock_is_released_when_the_briefing_fails(self):
        with mock.patch(
            "origin.views.public.mcp.views.tools.call", side_effect=RuntimeError("boom")
        ):
            failed = self._call()
        self.assertIn("error", failed.data)
        with mock.patch(
            "origin.views.public.mcp.views.tools.call",
            return_value={"content": [{"type": "text", "text": "ok"}], "isError": False},
        ):
            after = self._call()
        self.assertEqual(after.data["result"]["content"][0]["text"], "ok")

    def test_other_tools_are_never_gated(self):
        seen = {}

        def during(*_a, **_k):
            seen["res"] = self._call(name="get_current_user")
            return {"content": [{"type": "text", "text": "built"}], "isError": False}

        with mock.patch("origin.views.public.mcp.views.tools.call", side_effect=during):
            self._call()
        # The nested call is not the gated tool, so it ran (it reached `tools.call` and got "built").
        self.assertEqual(seen["res"].data["result"]["content"][0]["text"], "built")

    def test_cache_down_still_answers_the_briefing(self):
        with mock.patch(
            "origin.services.briefing_gate.throttle_cache", side_effect=OSError("redis down")
        ):
            res = self._call()
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("genos/briefing", res.data["result"].get("_meta", {}))

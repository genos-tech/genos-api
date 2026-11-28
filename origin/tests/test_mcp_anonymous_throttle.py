"""The per-address limit on MCP requests with no valid credential (MIP-1367 phase 1,
`views/public/anonymous_throttle.py`).

What is defended: refused requests are counted per address and, past the rate, answered 429 as a
JSON-RPC error; the address cannot be spoofed into someone else's bucket; and nothing that
authenticates is ever counted, so a shared address (a connector's egress, an office) cannot lock a
valid caller out. The 401 itself, below the limit, is byte-identical to `main`.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone
from django_redis.exceptions import ConnectionInterrupted
from rest_framework.request import Request
from rest_framework.throttling import BaseThrottle

from origin.models.common.api_key_models import ApiKey
from origin.services import throttles
from origin.services.throttles import denied_count
from origin.views.public import anonymous_throttle
from origin.views.public.anonymous_throttle import McpAnonymousThrottle, bucket_for
from origin.views.public.permissions import PublicApiThrottle

from .security_clock import MidMinute
from .test_mcp_endpoint import McpBase

BAD_KEY = "ApiKey gnos_not_a_real_key_0123456789"
PROXIED = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}


def _body(rid=1) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "ping"}).encode()


def _factory_request(remote, xff=None):
    extra = {"REMOTE_ADDR": remote}
    if xff:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


class Base(MidMinute, McpBase):
    # Both limiters count in fixed windows: a real clock crossing a minute between the three
    # allowed calls and the fourth lets the fourth through (MIP-2542). `throttles` is pinned too,
    # for the authenticated limit and the denial counter.
    clock_modules = (anonymous_throttle, throttles)

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # A cache error elsewhere opens a 5 s breaker that would make every later throttle allow.
        throttles._down_until = 0.0
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        patcher = mock.patch.object(McpAnonymousThrottle, "rate", "3/min")
        patcher.start()
        self.addCleanup(patcher.stop)

    def anon(self, *, ip="10.0.0.1", auth=None, rid=1, **extra):
        if auth:
            self.client.credentials(HTTP_AUTHORIZATION=auth)
        else:
            self.client.credentials()
        return self.client.post(
            self._url(), data=_body(rid), content_type="application/json", REMOTE_ADDR=ip, **extra
        )

    def exhaust(self, **kw):
        for _ in range(3):
            self.assertEqual(self.anon(**kw).status_code, 401)


class TestTheLimit(Base):
    def test_the_fourth_refused_call_in_a_minute_is_a_429_in_json_rpc(self):
        self.exhaust()
        res = self.anon(rid=99)
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res["Content-Type"], "application/json")
        wait = int(res["Retry-After"])
        self.assertTrue(1 <= wait <= 60)
        body = res.json()
        self.assertEqual(
            body,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "Too many requests without a valid credential from this address. "
                    f"Wait {wait} seconds, then connect again.",
                    "data": {"limit_reached": True, "category": "mcp_anonymous"},
                },
            },
        )

    def test_no_credential_and_a_refused_key_count_together(self):
        self.anon()
        self.anon(auth=BAD_KEY)
        self.anon()
        self.assertEqual(self.anon(auth=BAD_KEY).status_code, 429)

    def test_a_revoked_key_counts_too(self):
        raw = self._key()
        ApiKey.objects.update(revoked_at=timezone.now())
        self.exhaust(auth=f"ApiKey {raw}")
        self.assertEqual(self.anon(auth=f"ApiKey {raw}").status_code, 429)

    def test_the_limit_is_per_address(self):
        self.exhaust(ip="10.0.0.1")
        self.assertEqual(self.anon(ip="10.0.0.1").status_code, 429)
        self.assertEqual(self.anon(ip="10.0.0.2").status_code, 401)

    def test_the_429_is_answered_without_reading_the_body(self):
        self.exhaust()
        patcher = mock.patch.object(Request, "_parse", side_effect=AssertionError("parsed"))
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(self.anon().status_code, 429)
        spy.assert_not_called()

    def test_no_further_work_is_done_for_a_limited_call(self):
        self.exhaust(auth=BAD_KEY)
        with mock.patch(
            "origin.views.public.mcp.views.McpView.unauthorized_response",
            side_effect=AssertionError("answered as a plain 401"),
        ):
            self.assertEqual(self.anon(auth=BAD_KEY).status_code, 429)

    def test_a_denial_is_logged_and_counted_without_an_address_or_a_credential(self):
        self.exhaust(auth=BAD_KEY, ip="203.0.113.77")
        before = denied_count("mcp_anonymous")
        with self.assertLogs("origin.services.throttles", "WARNING") as logs:
            self.anon(auth=BAD_KEY, ip="203.0.113.77")
        text = "\n".join(logs.output)
        self.assertIn("throttle.denied scope=mcp_anonymous anonymous", text)
        self.assertNotIn("203.0.113.77", text)
        self.assertNotIn("gnos_", text)
        self.assertEqual(denied_count("mcp_anonymous"), before + 1)

    def test_the_window_turns_over(self):
        self.exhaust()
        self.assertEqual(self.anon().status_code, 429)
        later = mock.Mock(time=mock.Mock(return_value=4_000_000_000.0))
        with mock.patch.object(anonymous_throttle, "time", later):
            self.assertEqual(self.anon().status_code, 401)


class TestWhoIsNeverCounted(Base):
    def test_authenticated_calls_are_never_counted(self):
        raw = self._key()
        for _ in range(10):
            self.assertEqual(self.anon(auth=f"ApiKey {raw}").status_code, 200)
        # ...and they spent none of the address's allowance.
        self.exhaust()

    def test_an_exhausted_address_does_not_lock_out_a_valid_caller(self):
        # A connector's egress address, shared by many people: someone else's refused calls
        # fill the bucket, and a valid key from the same address still works.
        raw = self._key()
        self.exhaust(auth=BAD_KEY)
        self.assertEqual(self.anon(auth=BAD_KEY).status_code, 429)
        self.assertEqual(self.anon(auth=f"ApiKey {raw}").status_code, 200)
        self.assertEqual(self.anon(auth=f"ApiKey {raw}").json()["result"], {})

    def test_other_refusals_are_not_counted(self):
        raw = self._key()
        # A valid key refused for not being in the team (403) and a bad request (400) are not
        # "no valid credential".
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        listing = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for _ in range(5):
            res = self.client.post(
                "/api/public/v1/mcp?team_id=11111111-1111-1111-1111-111111111111",
                data=listing,
                content_type="application/json",
                REMOTE_ADDR="10.0.0.1",
            )
            self.assertEqual(res.status_code, 403)
        self.assertEqual(
            self.client.post(
                self._url(), data=b"[1]", content_type="application/json", REMOTE_ADDR="10.0.0.1"
            ).status_code,
            400,
        )
        self.exhaust()

    def test_the_authenticated_429_is_the_one_it_always_was(self):
        raw = self._key()
        with mock.patch.object(PublicApiThrottle, "rate", "2/min", create=True):
            self.assertEqual(self.anon(auth=f"ApiKey {raw}").status_code, 200)
            self.assertEqual(self.anon(auth=f"ApiKey {raw}").status_code, 200)
            res = self.anon(auth=f"ApiKey {raw}")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(list(res.json()), ["detail"])
        self.assertTrue(res.json()["detail"].startswith("Request was throttled."))
        self.assertIn("Retry-After", res)


class TestTheHandshakeIsUntouched(Base):
    """A client doing it right makes one refused call, then signs in. Below the limit the 401
    is exactly what it was."""

    def test_the_first_refusal_is_the_401_with_its_challenge(self):
        res = self.anon(rid=7)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":7,"error":{"code":-32600,"message":"Authentication credentials were not provided."}}',
        )
        self.assertEqual(res["WWW-Authenticate"], 'Bearer realm="api"')

    def test_the_oauth_challenge_is_still_there_on_every_unlimited_refusal(self):
        url = "https://api.example.test/.well-known/oauth-protected-resource"
        with override_settings(MCP_RESOURCE_METADATA_URL=url):
            first = self.anon(auth=BAD_KEY)
            third = None
            for _ in range(2):
                third = self.anon(auth=BAD_KEY)
        for res in (first, third):
            self.assertEqual(res.status_code, 401)
            self.assertEqual(
                res["WWW-Authenticate"], f'Bearer error="invalid_token", resource_metadata="{url}"'
            )

    def test_initialize_after_signing_in_is_never_limited(self):
        raw = self._key()
        self.exhaust(auth=BAD_KEY, ip="10.0.0.1")
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        res = self.client.post(
            self._url(),
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
            ),
            content_type="application/json",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(res.status_code, 200)


@override_settings(REST_FRAMEWORK=PROXIED)
class TestTheAddressCannotBeSpoofed(Base):
    def forwarded(self, header, remote="10.9.9.9"):
        return self.anon(ip=remote, HTTP_X_FORWARDED_FOR=header)

    def test_a_client_supplied_entry_does_not_choose_the_bucket(self):
        # Railway appends the address it saw (198.51.100.7) after whatever the client wrote.
        for i in range(3):
            self.assertEqual(self.forwarded(f"203.0.113.{i}, 198.51.100.7").status_code, 401)
        self.assertEqual(self.forwarded("203.0.113.200, 198.51.100.7").status_code, 429)

    def test_naming_someone_elses_address_does_not_spend_their_allowance(self):
        # The attacker (198.51.100.7 to the proxy) writes the victim's address into the header.
        victim = "198.51.100.50"
        for _ in range(5):
            self.forwarded(f"{victim}, 198.51.100.7")
        # It was the attacker's own bucket that filled; the victim's, as the proxy writes it
        # for them, is untouched.
        self.assertEqual(self.forwarded(victim).status_code, 401)
        self.assertEqual(self.forwarded("198.51.100.7").status_code, 429)

    def test_without_the_header_the_connection_address_is_used(self):
        self.exhaust(ip="198.51.100.8")
        self.assertEqual(self.anon(ip="198.51.100.8").status_code, 429)

    def test_the_address_the_proxy_wrote_is_the_bucket(self):
        self.assertEqual(bucket_for(self._request(xff="1.2.3.4, 198.51.100.7")), "198.51.100.7")

    def _request(self, xff=None, remote="10.9.9.9"):
        return _factory_request(remote, xff)


class TestBucket(SimpleTestCase):
    def req(self, remote, xff=None):
        return _factory_request(remote, xff)

    @override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 0})
    def test_ipv4_is_itself(self):
        self.assertEqual(bucket_for(self.req("192.0.2.1")), "192.0.2.1")

    @override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 0})
    def test_ipv6_shares_its_slash_64(self):
        a = bucket_for(self.req("2001:db8:1:2:aaaa::1"))
        b = bucket_for(self.req("2001:db8:1:2:bbbb::9"))
        c = bucket_for(self.req("2001:db8:1:3::1"))
        self.assertEqual(a, "2001:db8:1:2::/64")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    @override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 0})
    def test_something_that_is_not_an_address_is_one_shared_bucket(self):
        self.assertEqual(bucket_for(self.req("not-an-ip")), "unknown")
        self.assertEqual(bucket_for(RequestFactory().get("/", REMOTE_ADDR="")), "unknown")

    def test_it_is_the_resolution_every_other_throttle_uses(self):
        request = self.req("192.0.2.9")
        self.assertEqual(bucket_for(request), BaseThrottle().get_ident(request))


class TestTheRate(SimpleTestCase):
    def test_default_is_300_a_minute(self):
        self.assertEqual(anonymous_throttle.DEFAULT_RATE, "300/min")
        self.assertEqual(McpAnonymousThrottle.rate, "300/min")
        self.assertEqual(McpAnonymousThrottle().num_requests, 300)
        self.assertEqual(McpAnonymousThrottle().duration, 60)

    def test_the_environment_can_change_it_and_a_typo_cannot_break_it(self):
        for raw, want in (
            ("", "300/min"),
            ("lots", "300/min"),
            ("5/", "300/min"),
            ("1/2/3", "300/min"),
            ("/min", "300/min"),
            ("40/hour", "40/hour"),
        ):
            with mock.patch.dict("os.environ", {"THROTTLE_MCP_ANONYMOUS": raw}):
                self.assertEqual(anonymous_throttle._configured_rate(), want, raw)

    def test_the_rate_is_not_in_settings(self):
        # Keeps the PR clear of the billed agent-evals trigger (`apis/settings.py`).
        self.assertNotIn("mcp_anonymous", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])


class TestWhenTheCacheIsDown(Base):
    def test_it_allows_and_says_so(self):
        with mock.patch.object(
            throttles, "throttle_cache", side_effect=ConnectionInterrupted("down")
        ):
            with self.assertLogs("origin.services.throttles", "WARNING") as logs:
                for _ in range(6):
                    self.assertEqual(self.anon().status_code, 401)
        self.assertIn(
            "throttle.cache_unavailable scope=mcp_anonymous action=allow", "\n".join(logs.output)
        )

"""Real client address behind Cloudflare (MIP-2235)."""

from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.throttling import BaseThrottle

from origin.middleware import real_client_ip as m
from origin.services.security_events import client_ip

CF_EDGE = "172.69.10.20"  # inside 172.64.0.0/13
CF_EDGE_V6 = "2606:4700:10::1"
RAILWAY_ONLY = "198.51.100.7"  # not Cloudflare
VISITOR = "203.0.113.9"

REST_ONE_PROXY = {"NUM_PROXIES": 1}


def _through(headers: dict):
    """Run a request through the middleware and return the request it hands on."""
    seen = {}

    def view(request):
        seen["request"] = request
        return mock.Mock()

    request = RequestFactory().get("/x", **headers)
    m.RealClientIPMiddleware(view)(request)
    return seen["request"]


class ResolverTests(SimpleTestCase):
    def test_cloudflare_peer_with_header_uses_header(self):
        self.assertEqual(m.resolve_forwarded_for(f"{CF_EDGE}", VISITOR), VISITOR)

    def test_client_written_entries_before_the_proxy_are_dropped(self):
        self.assertEqual(m.resolve_forwarded_for(f"1.2.3.4, {CF_EDGE}", VISITOR), VISITOR)

    def test_ipv6_edge_and_ipv6_visitor(self):
        self.assertEqual(m.resolve_forwarded_for(CF_EDGE_V6, "2001:db8::5"), "2001:db8::5")

    def test_header_from_a_non_cloudflare_peer_is_ignored(self):
        # Someone who bypassed Cloudflare and sent CF-Connecting-IP themselves.
        self.assertIsNone(m.resolve_forwarded_for(RAILWAY_ONLY, "9.9.9.9"))

    def test_forged_cloudflare_address_earlier_in_the_list_is_not_enough(self):
        # Only the entry the proxy wrote (the last) counts.
        self.assertIsNone(m.resolve_forwarded_for(f"{CF_EDGE}, {RAILWAY_ONLY}", "9.9.9.9"))

    def test_missing_or_invalid_values_leave_the_request_alone(self):
        self.assertIsNone(m.resolve_forwarded_for(None, VISITOR))
        self.assertIsNone(m.resolve_forwarded_for(CF_EDGE, None))
        self.assertIsNone(m.resolve_forwarded_for(CF_EDGE, "not-an-ip"))
        self.assertIsNone(m.resolve_forwarded_for(CF_EDGE, f"{VISITOR}, 1.1.1.1"))
        self.assertIsNone(m.resolve_forwarded_for("garbage", VISITOR))

    def test_range_boundaries(self):
        self.assertTrue(m.is_cloudflare_address("104.16.0.1"))
        self.assertTrue(m.is_cloudflare_address("104.23.255.254"))
        self.assertFalse(m.is_cloudflare_address("104.15.255.255"))
        self.assertTrue(m.is_cloudflare_address("::ffff:172.64.0.1"))
        self.assertFalse(m.is_cloudflare_address(""))


@override_settings(REST_FRAMEWORK={**REST_ONE_PROXY})
class MiddlewareTests(SimpleTestCase):
    def test_throttle_identity_is_the_visitor_not_the_edge(self):
        request = _through(
            {"HTTP_X_FORWARDED_FOR": f"6.6.6.6, {CF_EDGE}", "HTTP_CF_CONNECTING_IP": VISITOR}
        )
        self.assertEqual(BaseThrottle().get_ident(request), VISITOR)
        self.assertEqual(client_ip(request), VISITOR)

    def test_two_visitors_on_one_edge_get_two_identities(self):
        a = _through({"HTTP_X_FORWARDED_FOR": CF_EDGE, "HTTP_CF_CONNECTING_IP": "203.0.113.1"})
        b = _through({"HTTP_X_FORWARDED_FOR": CF_EDGE, "HTTP_CF_CONNECTING_IP": "203.0.113.2"})
        self.assertNotEqual(BaseThrottle().get_ident(a), BaseThrottle().get_ident(b))

    def test_direct_hit_cannot_choose_its_address(self):
        request = _through(
            {"HTTP_X_FORWARDED_FOR": f"7.7.7.7, {RAILWAY_ONLY}", "HTTP_CF_CONNECTING_IP": "9.9.9.9"}
        )
        self.assertEqual(BaseThrottle().get_ident(request), RAILWAY_ONLY)

    def test_without_cloudflare_nothing_changes(self):
        request = _through({"HTTP_X_FORWARDED_FOR": f"7.7.7.7, {RAILWAY_ONLY}"})
        self.assertEqual(request.META["HTTP_X_FORWARDED_FOR"], f"7.7.7.7, {RAILWAY_ONLY}")

    def test_kill_switch(self):
        with mock.patch.dict("os.environ", {"TRUST_CLOUDFLARE_IP": "0"}):
            request = _through({"HTTP_X_FORWARDED_FOR": CF_EDGE, "HTTP_CF_CONNECTING_IP": VISITOR})
        self.assertEqual(BaseThrottle().get_ident(request), CF_EDGE)


class WiringTests(SimpleTestCase):
    def test_installed_before_anything_that_reads_the_address(self):
        from django.conf import settings

        mw = settings.MIDDLEWARE
        ours = "origin.middleware.real_client_ip.RealClientIPMiddleware"
        self.assertIn(ours, mw)
        self.assertLess(
            mw.index(ours), mw.index("origin.services.ops_alerts.ServerErrorBurstMiddleware")
        )

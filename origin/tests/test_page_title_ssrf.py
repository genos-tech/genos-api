"""`getPageTitle` and the outbound-URL guard behind it (MIP-717).

The endpoint was unauthenticated and fetched any URL, so anyone could
make the API probe its private network. What has to hold:

* Only a signed-in caller gets a fetch, and it is throttled.
* No request leaves for a non-public address: private, loopback,
  link-local / metadata, CGNAT, and IPv6 forms that carry one of those.
* The connection goes to the IP that was checked, never a second DNS
  answer (rebinding), and every redirect hop is checked again.
* Only http(s) on the standard ports; no credentials in the URL.
* Network errors are never echoed back.
"""

from __future__ import annotations

import ipaddress
import socket
from unittest import mock

from django.core.cache import cache
from urllib3.exceptions import NewConnectionError

from origin.services import outbound_url
from origin.services.outbound_url import OutboundUrlError, fetch_public, is_public_ip

from .test_base import BaseAPITestCase

URL = "/api/v2/getPageTitle/"
PUBLIC = "93.184.216.34"


def _dns(*ips):
    family = {4: socket.AF_INET, 6: socket.AF_INET6}
    return mock.patch(
        "origin.services.outbound_url.socket.getaddrinfo",
        return_value=[(family[ipaddress.ip_address(ip).version], 1, 6, "", (ip, 0)) for ip in ips],
    )


class _FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunk=None, greedy=False):
        self.status = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._body = body
        self._chunk = chunk
        self._greedy = greedy
        self.reads = 0

    def read(self, n, decode_content=True):
        self.reads += 1
        n = self._chunk if self._greedy else min(n, self._chunk or n)
        out, self._body = self._body[:n], self._body[n:]
        return out

    def release_conn(self):
        pass

    def close(self):
        pass


class _Pools:
    """Records which (scheme, ip, port, host header, sni) each request
    went to, and answers from a script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _pool(self, scheme):
        pools = self

        class Pool:
            def __init__(self, host, port, **kw):
                self.host, self.port, self.kw = host, port, kw

            def urlopen(self, method, path, headers=None, **kw):
                pools.calls.append(
                    {
                        "scheme": scheme,
                        "ip": self.host,
                        "port": self.port,
                        "path": path,
                        "host": headers.get("Host"),
                        "sni": self.kw.get("server_hostname"),
                        "assert_hostname": self.kw.get("assert_hostname"),
                        "cert_reqs": self.kw.get("cert_reqs"),
                        "redirect": kw.get("redirect"),
                    }
                )
                return pools.responses.pop(0)

            def close(self):
                pass

        return Pool

    def __enter__(self):
        self._patches = [
            mock.patch.object(outbound_url.urllib3, "HTTPConnectionPool", self._pool("http")),
            mock.patch.object(outbound_url.urllib3, "HTTPSConnectionPool", self._pool("https")),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _html(title="Example Domain"):
    return _FakeResponse(body=f"<html><head><title>{title}</title></head></html>".encode())


class IsPublicIpTests(BaseAPITestCase):
    def test_public_addresses_pass(self):
        for ip in ("93.184.216.34", "8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"):
            with self.subTest(ip=ip):
                self.assertTrue(is_public_ip(ipaddress.ip_address(ip)))

    def test_every_internal_form_is_refused(self):
        for ip in (
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.0.1",
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # shared / CGNAT — `is_private` is False for it
            "0.0.0.0",
            "0.1.2.3",
            "198.18.0.1",
            "224.0.0.1",
            "240.0.0.1",
            "::1",
            "::",
            "fd00::1",
            "fe80::1",
            "fec0::1",
            "::ffff:127.0.0.1",  # IPv4-mapped
            "::ffff:169.254.169.254",
            "64:ff9b::7f00:1",  # NAT64 127.0.0.1 — `is_global` says True; reserved
            "64:ff9b::a9fe:a9fe",  # NAT64 169.254.169.254
            "2002:7f00:1::1",  # 6to4 of 127.0.0.1
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
            "2001:db8::1",
        ):
            with self.subTest(ip=ip):
                self.assertFalse(is_public_ip(ipaddress.ip_address(ip)))


class FetchPublicTests(BaseAPITestCase):
    def assertRefused(self, url, *ips):
        with _dns(*(ips or (PUBLIC,))), _Pools() as pools:
            with self.assertRaises(OutboundUrlError):
                fetch_public(url)
        self.assertEqual(pools.calls, [], "a refused URL must not be fetched")

    def test_a_public_page_is_fetched_by_its_checked_ip(self):
        """The pool is opened on the IP that was checked — not on the
        hostname, which `requests` would resolve a second time."""
        with _dns(PUBLIC), _Pools(_html()) as pools:
            result = fetch_public("https://example.com/a?b=1")
        call = pools.calls[0]
        self.assertEqual(call["ip"], PUBLIC)
        self.assertEqual(call["port"], 443)
        self.assertEqual(call["path"], "/a?b=1")
        self.assertEqual(call["host"], "example.com")
        self.assertEqual(call["redirect"], False)
        self.assertIn(b"Example Domain", result.body)

    def test_tls_is_verified_against_the_name_not_the_ip(self):
        with _dns(PUBLIC), _Pools(_html()) as pools:
            fetch_public("https://example.com/")
        call = pools.calls[0]
        self.assertEqual(call["sni"], "example.com")
        self.assertEqual(call["assert_hostname"], "example.com")
        self.assertEqual(call["cert_reqs"], "CERT_REQUIRED")

    def test_internal_addresses_are_never_contacted(self):
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "fec0::1"):
            with self.subTest(ip=ip):
                self.assertRefused("http://internal.example/", ip)

    def test_a_literal_internal_ip_is_refused(self):
        with mock.patch(
            "origin.services.outbound_url.socket.getaddrinfo", wraps=socket.getaddrinfo
        ):
            for url in ("http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest"):
                with self.subTest(url=url), _Pools() as pools:
                    with self.assertRaises(OutboundUrlError):
                        fetch_public(url)
                    self.assertEqual(pools.calls, [])

    def test_one_private_record_is_enough_to_refuse(self):
        self.assertRefused("https://mixed.example/", PUBLIC, "10.0.0.5")

    def test_only_http_and_https(self):
        for url in ("file:///etc/passwd", "gopher://example.com/", "ftp://example.com/", "//x"):
            with self.subTest(url=url):
                self.assertRefused(url)

    def test_only_standard_ports(self):
        """A port is how you'd scan a public host's services."""
        for url in (
            "http://example.com:9200/",
            "https://example.com:6379/",
            "http://example.com:443/",
        ):
            with self.subTest(url=url):
                self.assertRefused(url)

    def test_explicit_standard_ports_are_fine(self):
        with _dns(PUBLIC), _Pools(_html()) as pools:
            fetch_public("http://example.com:80/")
        self.assertEqual(pools.calls[0]["port"], 80)

    def test_credentials_in_the_url_are_refused(self):
        self.assertRefused("https://user:pw@example.com/")

    def test_a_redirect_to_an_internal_address_is_not_followed(self):
        """The check has to run on every hop, not only on what was typed."""
        dns = mock.patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            side_effect=lambda host, *a, **k: [
                (
                    socket.AF_INET,
                    1,
                    6,
                    "",
                    ("10.0.0.9" if host == "internal.example" else PUBLIC, 0),
                )
            ],
        )
        redirect = _FakeResponse(status=302, headers={"Location": "http://internal.example/"})
        with dns, _Pools(redirect) as pools:
            with self.assertRaises(OutboundUrlError):
                fetch_public("https://example.com/")
        self.assertEqual(len(pools.calls), 1)

    def test_a_public_redirect_is_followed_and_checked(self):
        redirect = _FakeResponse(status=301, headers={"Location": "/new"})
        with _dns(PUBLIC), _Pools(redirect, _html()) as pools:
            result = fetch_public("https://example.com/old")
        self.assertEqual([c["path"] for c in pools.calls], ["/old", "/new"])
        self.assertEqual(result.url, "https://example.com/new")

    def test_redirects_are_capped(self):
        loops = [_FakeResponse(status=302, headers={"Location": "/again"}) for _ in range(5)]
        with _dns(PUBLIC), _Pools(*loops) as pools:
            with self.assertRaises(OutboundUrlError):
                fetch_public("https://example.com/", max_redirects=3)
        self.assertEqual(len(pools.calls), 4)

    def test_the_body_is_capped(self):
        """The fake hands back a full 4 KB chunk per read whatever size was
        asked for, like a server that keeps sending: only the loop's own
        bound stops it."""
        big = _FakeResponse(body=b"x" * 100_000, chunk=4096, greedy=True)
        with _dns(PUBLIC), _Pools(big):
            result = fetch_public("https://example.com/", max_bytes=1000)
        self.assertLessEqual(len(result.body), 4096)
        self.assertLess(big.reads, 3)

    def test_a_slow_drip_hits_the_overall_deadline(self):
        """Each read can be under the socket timeout; the call as a whole
        still has to end."""
        drip = _FakeResponse(body=b"x" * 10_000, chunk=1)
        clock = iter(range(0, 10_000))
        with (
            _dns(PUBLIC),
            _Pools(drip),
            mock.patch.object(outbound_url.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            with self.assertRaises(OutboundUrlError):
                fetch_public("https://example.com/", timeout=5)
        self.assertLess(drip.reads, 10)


class PageTitleViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_an_anonymous_caller_is_refused_before_any_fetch(self):
        with mock.patch("origin.views.utils.extract_page_title_view.fetch_public") as fetch:
            resp = self.client.get(URL, {"url": "http://169.254.169.254/"})
        self.assertEqual(resp.status_code, 401)
        fetch.assert_not_called()

    def test_a_signed_in_caller_gets_the_title(self):
        self.authenticate()
        with _dns(PUBLIC), _Pools(_html("Fish &amp; Chips\n  — Menu")):
            resp = self.client.get(URL, {"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Fish & Chips — Menu")

    def test_an_internal_url_is_refused_with_a_plain_reason(self):
        self.authenticate()
        with _dns("10.0.0.5"), _Pools() as pools:
            resp = self.client.get(URL, {"url": "http://opensearch.railway.internal:9200/"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(pools.calls, [])

    def test_a_network_error_is_not_echoed(self):
        """`str(e)` used to go back verbatim — the response itself was
        the port scanner's readout."""
        self.authenticate()

        class Boom:
            def __init__(self, *a, **k):
                pass

            def urlopen(self, *a, **k):
                raise NewConnectionError(None, "connect to 203.0.113.7 port 443 refused")

            def close(self):
                pass

        with _dns(PUBLIC), mock.patch.object(outbound_url.urllib3, "HTTPSConnectionPool", Boom):
            resp = self.client.get(URL, {"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["title"])
        self.assertNotIn("refused", resp.content.decode())
        self.assertNotIn("error", resp.json())

    def test_a_non_html_answer_has_no_title(self):
        self.authenticate()
        json_resp = _FakeResponse(
            body=b'{"title": "<title>x</title>"}', headers={"Content-Type": "application/json"}
        )
        with _dns(PUBLIC), _Pools(json_resp):
            resp = self.client.get(URL, {"url": "https://example.com/api"})
        self.assertIsNone(resp.json()["title"])

    def test_a_missing_url_is_a_400(self):
        self.authenticate()
        self.assertEqual(self.client.get(URL).status_code, 400)

    def test_it_is_throttled_per_user(self):
        self.authenticate()
        with mock.patch(
            "origin.views.utils.extract_page_title_view.fetch_public",
            side_effect=OutboundUrlError("x"),
        ):
            codes = [
                self.client.get(URL, {"url": f"http://10.0.0.{i}/"}).status_code for i in range(31)
            ]
        self.assertEqual(codes[:30], [400] * 30)
        self.assertEqual(codes[30], 429)

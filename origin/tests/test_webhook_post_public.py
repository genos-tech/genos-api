"""A webhook delivery connects to the address that was checked (MIP-819).

CodeQL raised `py/full-ssrf` on `post_delivery`, and reading it showed a real gap
under the alert: the destination was checked (`is_public_host` resolves the name)
and then `requests.post(url)` resolved it AGAIN to connect. A hostname with a
short TTL can answer publicly for the check and privately for the connection,
and the delivery then POSTs a signed body into the private network (Postgres,
Redis, OpenSearch, the metadata endpoint).

`post_delivery` now goes through `outbound_url.post_public`, which resolves once
and connects to that checked address. What has to hold, and is pinned here:

* the connection goes to the IP that was checked, never to a second answer;
* a rebind between the shape check and the send reaches nothing;
* one private record among public ones refuses the whole delivery;
* a redirect is a status, not a hop;
* https and the standard port only, no credentials;
* the signed body and headers reach the wire unchanged, as a POST;
* the reply is never read.

Fakes are the page-title tests' (`_Pools`, `_dns`), extended to record the
method and the body.
"""

from __future__ import annotations

import socket
from unittest import mock

import urllib3

from origin.services import outbound_url, webhook_delivery
from origin.services.outbound_url import OutboundUrlError, post_public
from origin.services.webhook_delivery import post_delivery, sign

from .test_base import BaseAPITestCase
from .test_page_title_ssrf import PUBLIC, _dns, _FakeResponse, _Pools

URL = "https://hooks.example.com/genos"
SECRET = "whsec_test"
PAYLOAD = {"event": "task.created", "id": 7}


class _PostPools(_Pools):
    """`_Pools` that also records the method and the body that were sent."""

    def _pool(self, scheme):
        pools = self
        base = super()._pool(scheme)

        class Pool(base):
            def urlopen(self, method, path, body=None, headers=None, **kw):
                result = super().urlopen(method, path, headers=headers, **kw)
                pools.calls[-1].update(method=method, body=body, headers=dict(headers or {}))
                return result

        return Pool


def _reply(status=200):
    return _FakeResponse(status=status, body=b"x" * 100_000, chunk=1024)


class PostPublicTests(BaseAPITestCase):
    def test_it_posts_to_the_checked_ip_not_the_name(self):
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            status = post_public(URL, b"{}", headers={"X-Test": "1"})

        self.assertEqual(status, 200)
        (call,) = pools.calls
        self.assertEqual(call["ip"], PUBLIC)
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["host"], "hooks.example.com")

    def test_the_certificate_is_still_checked_against_the_typed_name(self):
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            post_public(URL, b"{}", headers={})

        (call,) = pools.calls
        self.assertEqual(call["sni"], "hooks.example.com")
        self.assertEqual(call["assert_hostname"], "hooks.example.com")
        self.assertEqual(call["cert_reqs"], "CERT_REQUIRED")

    def test_the_name_is_looked_up_exactly_once(self):
        """The whole fix. Two lookups are two chances to be rebound."""
        with _dns(PUBLIC) as dns, _PostPools(_reply()):
            post_public(URL, b"{}", headers={})

        self.assertEqual(dns.call_count, 1)

    def test_a_rebind_after_the_check_cannot_redirect_the_connection(self):
        """The first lookup is public, every later one private. The connection
        must still go to the public address that was checked."""
        answers = iter([PUBLIC, "169.254.169.254", "10.0.0.5", "127.0.0.1"])

        def rebinding(host, *a, **k):
            return [(socket.AF_INET, 1, 6, "", (next(answers), 0))]

        with (
            mock.patch("origin.services.outbound_url.socket.getaddrinfo", side_effect=rebinding),
            _PostPools(_reply()) as pools,
        ):
            post_public(URL, b"{}", headers={})

        self.assertEqual([c["ip"] for c in pools.calls], [PUBLIC])

    def test_a_private_address_is_refused_and_never_contacted(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "100.64.0.1"):
            with self.subTest(ip=ip), _dns(ip), _PostPools() as pools:
                with self.assertRaises(OutboundUrlError):
                    post_public(URL, b"{}", headers={})
                self.assertEqual(pools.calls, [])

    def test_one_private_record_among_public_ones_refuses_the_delivery(self):
        with _dns(PUBLIC, "10.0.0.5"), _PostPools() as pools:
            with self.assertRaises(OutboundUrlError):
                post_public(URL, b"{}", headers={})
            self.assertEqual(pools.calls, [])

    def test_a_host_that_does_not_resolve_is_refused(self):
        with mock.patch(
            "origin.services.outbound_url.socket.getaddrinfo", side_effect=socket.gaierror
        ):
            with self.assertRaises(OutboundUrlError):
                post_public(URL, b"{}", headers={})

    def test_https_only(self):
        for url in ("http://hooks.example.com/x", "ftp://hooks.example.com/x"):
            with self.subTest(url=url), _dns(PUBLIC), _PostPools() as pools:
                with self.assertRaises(OutboundUrlError):
                    post_public(url, b"{}", headers={})
                self.assertEqual(pools.calls, [])

    def test_only_the_standard_port(self):
        with _dns(PUBLIC), _PostPools() as pools:
            with self.assertRaises(OutboundUrlError):
                post_public("https://hooks.example.com:6379/x", b"{}", headers={})
            self.assertEqual(pools.calls, [])

    def test_the_explicit_standard_port_is_fine(self):
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            post_public("https://hooks.example.com:443/x", b"{}", headers={})

        self.assertEqual(pools.calls[0]["port"], 443)

    def test_credentials_in_the_url_are_refused(self):
        with _dns(PUBLIC), _PostPools() as pools:
            with self.assertRaises(OutboundUrlError):
                post_public("https://user:pw@hooks.example.com/x", b"{}", headers={})
            self.assertEqual(pools.calls, [])

    def test_a_redirect_is_returned_as_a_status_and_not_followed(self):
        with (
            _dns(PUBLIC),
            _PostPools(
                _FakeResponse(status=302, headers={"Location": "http://169.254.169.254/latest/"})
            ) as pools,
        ):
            status = post_public(URL, b"{}", headers={})

        self.assertEqual(status, 302)
        self.assertEqual(len(pools.calls), 1)
        self.assertIs(pools.calls[0]["redirect"], False)

    def test_the_body_and_headers_are_sent_unchanged(self):
        body = b'{"event":"task.created"}'
        headers = {"Content-Type": "application/json", "X-Genos-Signature": "sha256=abc"}
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            post_public(URL, body, headers=headers)

        (call,) = pools.calls
        self.assertEqual(call["body"], body)
        self.assertEqual(call["headers"]["X-Genos-Signature"], "sha256=abc")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Host"], "hooks.example.com")

    def test_the_path_and_query_are_kept(self):
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            post_public("https://hooks.example.com/a/b?x=1&y=2", b"{}", headers={})

        self.assertEqual(pools.calls[0]["path"], "/a/b?x=1&y=2")

    def test_the_reply_is_never_read(self):
        """Only the status matters; a customer's endpoint must not be able to
        make a worker buffer an unbounded body."""
        reply = _reply()
        with _dns(PUBLIC), _PostPools(reply):
            post_public(URL, b"{}", headers={})

        self.assertEqual(reply.reads, 0)

    def test_the_connection_is_released_even_when_it_succeeds(self):
        reply = _reply()
        released = []
        reply.release_conn = lambda: released.append("release")
        reply.close = lambda: released.append("close")
        with _dns(PUBLIC), _PostPools(reply):
            post_public(URL, b"{}", headers={})

        self.assertEqual(released, ["release", "close"])


class PostDeliveryTests(BaseAPITestCase):
    """`post_delivery` keeps its contract: `(status, message)`, never raises."""

    def _send(self):
        return post_delivery(URL, SECRET, "task.created", "d-1", PAYLOAD)

    def test_a_2xx_is_success_with_no_message(self):
        with _dns(PUBLIC), _PostPools(_reply(204)):
            self.assertEqual(self._send(), (204, ""))

    def test_a_non_2xx_is_reported_with_its_status(self):
        for status in (301, 400, 404, 500):
            with self.subTest(status=status), _dns(PUBLIC), _PostPools(_reply(status)):
                self.assertEqual(self._send(), (status, f"HTTP {status}"))

    def test_a_private_destination_is_a_message_not_a_request_or_a_raise(self):
        with _dns("10.0.0.5"), _PostPools() as pools:
            status, message = self._send()

        self.assertIsNone(status)
        self.assertIn("public", message.lower())
        self.assertEqual(pools.calls, [])

    def test_a_rebind_between_the_shape_check_and_the_send_reaches_nothing(self):
        """`validate_webhook_url` resolves the name once (public); the send
        resolves it again and now gets a private answer. Nothing may be sent."""
        answers = iter([PUBLIC, "10.0.0.5", "10.0.0.5"])

        def rebinding(host, *a, **k):
            return [(socket.AF_INET, 1, 6, "", (next(answers), 0))]

        with (
            mock.patch("origin.services.outbound_url.socket.getaddrinfo", side_effect=rebinding),
            _PostPools(_reply()) as pools,
        ):
            status, message = self._send()

        self.assertIsNone(status)
        self.assertEqual(pools.calls, [])
        self.assertTrue(message)

    def test_the_signed_body_is_what_goes_on_the_wire(self):
        with _dns(PUBLIC), _PostPools(_reply()) as pools:
            self._send()

        (call,) = pools.calls
        sent_headers = call["headers"]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            sent_headers["X-Genos-Signature"],
            sign(SECRET, sent_headers["X-Genos-Timestamp"], call["body"]),
        )
        self.assertEqual(sent_headers["X-Genos-Event"], "task.created")
        self.assertEqual(sent_headers["X-Genos-Delivery"], "d-1")
        self.assertEqual(sent_headers["User-Agent"], "Genos-Webhooks/1")

    def test_a_network_failure_is_a_message_not_a_raise(self):
        class Boom:
            def __init__(self, *a, **k):
                raise urllib3.exceptions.NewConnectionError(None, "boom")

        with _dns(PUBLIC), mock.patch.object(outbound_url.urllib3, "HTTPSConnectionPool", Boom):
            status, message = self._send()

        self.assertIsNone(status)
        self.assertTrue(message)
        self.assertLessEqual(len(message), 300)

    def test_an_oserror_is_a_message_not_a_raise(self):
        class Boom:
            def __init__(self, *a, **k):
                raise OSError("network unreachable")

        with _dns(PUBLIC), mock.patch.object(outbound_url.urllib3, "HTTPSConnectionPool", Boom):
            status, message = self._send()

        self.assertIsNone(status)
        self.assertIn("unreachable", message)

    def test_http_is_still_refused_with_the_form_message(self):
        status, message = post_delivery(
            "http://hooks.example.com/x", SECRET, "task.created", "d-1", PAYLOAD
        )

        self.assertIsNone(status)
        self.assertIn("https", message)

    def test_it_no_longer_goes_through_requests(self):
        """The second lookup lived in `requests`. If it comes back, so does the
        rebinding gap."""
        self.assertFalse(hasattr(webhook_delivery, "requests"))

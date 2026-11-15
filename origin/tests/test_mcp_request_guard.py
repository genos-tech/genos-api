"""The MCP request-size guard (MIP-1367 phase 1, `mcp/request_guard.py`).

Two things are defended. The new refusals: a request over the limit is answered 413 as a JSON-RPC
error before anything authenticates or parses it, with no traceback and no content in the log; and
a body without a `Content-Length` is answered 411 instead of being read as empty. And the old
behaviour, which must not move: every request up to the limit, and every answer that is not one of
those two refusals, is byte-identical to what `main` sent (the literals below were captured from
`main` and are pinned here).
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import override_settings
from rest_framework.request import Request

from origin.views.public.mcp import request_guard

from .test_mcp_endpoint import MCP, McpBase

LIMIT = 2_621_440
LOGGER = "origin.views.public.mcp.request_guard"
SECRET = "SECRET-WORKSPACE-TEXT-9f3a"


def _ping_body(total: int | None = None, rid=1) -> bytes:
    """A valid JSON-RPC ping, padded with an ignored field to exactly `total` bytes."""
    base = {"jsonrpc": "2.0", "id": rid, "method": "ping"}
    if total is None:
        return json.dumps(base, separators=(",", ":")).encode()
    base["pad"] = ""
    empty = len(json.dumps(base, separators=(",", ":")).encode())
    assert total >= empty
    base["pad"] = SECRET[:1] * (total - empty)
    raw = json.dumps(base, separators=(",", ":")).encode()
    assert len(raw) == total
    return raw


class Guard(McpBase):
    def parse_spy(self):
        """Patches DRF's body parse and returns the mock; assert it was never called. (An
        exception from inside it would be swallowed by the view's `except Exception`.)"""
        patcher = mock.patch.object(Request, "_parse", side_effect=AssertionError("parsed"))
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def post_raw(self, body: bytes, url: str | None = None, **extra):
        return self.client.post(
            url or self._url(), data=body, content_type="application/json", **extra
        )


class TestTooLarge(Guard):
    def test_over_the_default_limit_is_413_with_a_message_to_act_on(self):
        self._key()
        res = self.post_raw(_ping_body(LIMIT + 1))
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res["Content-Type"], "application/json")
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":'
            b'"The request is 2621441 bytes; the limit is 2.5 MiB. Send less text per call."}}',
        )

    def test_a_request_exactly_at_the_limit_is_read_as_before(self):
        self._key()
        res = self.post_raw(_ping_body(LIMIT))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b'{"jsonrpc":"2.0","id":1,"result":{}}')

    def test_the_boundary_follows_the_constant(self):
        self._key()
        with mock.patch.object(request_guard, "MCP_MAX_REQUEST_BYTES", 5000):
            self.assertEqual(self.post_raw(_ping_body(5000)).status_code, 200)
            res = self.post_raw(_ping_body(5001))
        self.assertEqual(res.status_code, 413)
        self.assertIn(
            "The request is 5001 bytes; the limit is 4.88281 KiB.", res.json()["error"]["message"]
        )

    def test_the_default_is_todays_limit(self):
        # Not a new number: Django's own default, so nothing that worked before is refused.
        self.assertEqual(request_guard.DEFAULT_MAX_REQUEST_BYTES, 2_621_440)
        self.assertEqual(request_guard.effective_limit(), 2_621_440)

    def test_refused_before_authentication(self):
        # No credential at all: the size answer comes first, and no query is made.
        with self.assertNumQueries(0):
            res = self.post_raw(_ping_body(LIMIT + 1))
        self.assertEqual(res.status_code, 413)

    def test_refused_before_a_key_is_looked_up(self):
        self.client.credentials(HTTP_AUTHORIZATION="ApiKey gnos_not_a_real_key")
        with self.assertNumQueries(0):
            res = self.post_raw(_ping_body(LIMIT + 1))
        self.assertEqual(res.status_code, 413)

    def test_refused_before_the_body_is_parsed(self):
        self._key()
        spy = self.parse_spy()
        res = self.post_raw(_ping_body(LIMIT + 1))
        self.assertEqual(res.status_code, 413)
        spy.assert_not_called()

    def test_refused_while_mcp_is_switched_off_too(self):
        with override_settings(MCP_DISABLED=True):
            res = self.post_raw(_ping_body(LIMIT + 1))
        self.assertEqual(res.status_code, 413)

    def test_no_traceback_no_error_log_and_no_content_in_the_log(self):
        self._key()
        with self.assertNoLogs(level="ERROR"):
            with self.assertLogs(LOGGER, "INFO") as logs:
                self.post_raw(_ping_body(LIMIT + 1))
        text = "\n".join(logs.output)
        self.assertIn("reason=too_large bytes=2621441 limit=2621440", text)
        self.assertNotIn(SECRET[:1] * 50, text)
        self.assertNotIn("gnos_", text)

    def test_an_oversized_body_with_an_array_or_garbage_is_still_just_too_large(self):
        for raw in (b"[" + b"1," * 2_000_000 + b"1]", b"\xff" * (LIMIT + 5)):
            self.assertEqual(self.post_raw(raw).status_code, 413)

    def test_a_non_post_is_not_the_guards_business(self):
        self._key()
        res = self.client.get(self._url(), CONTENT_LENGTH=str(LIMIT + 1))
        self.assertEqual(res.status_code, 405)

    def test_an_unreadable_length_is_not_a_refusal(self):
        # Django reads it as no body; so does the guard. Same as before.
        self._key()
        res = self.post_raw(b"", CONTENT_LENGTH="not-a-number")
        self.assertNotEqual(res.status_code, 413)

    def test_effective_limit_is_never_above_djangos(self):
        with mock.patch.object(request_guard, "MCP_MAX_REQUEST_BYTES", 10_000_000):
            self.assertEqual(request_guard.effective_limit(), 2_621_440)
            with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1000):
                self.assertEqual(request_guard.effective_limit(), 1000)
            with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=None):
                self.assertEqual(request_guard.effective_limit(), 10_000_000)

    def test_env_values_that_are_not_a_positive_number_keep_the_default(self):
        for raw, want in (
            ("", 2_621_440),
            ("abc", 2_621_440),
            ("0", 2_621_440),
            ("-5", 2_621_440),
            ("1000", 1000),
        ):
            with mock.patch.dict("os.environ", {"MCP_MAX_REQUEST_BYTES": raw}):
                self.assertEqual(request_guard._env_int("MCP_MAX_REQUEST_BYTES", 2_621_440), want)

    def test_sizes_are_written_for_a_person(self):
        self.assertEqual(request_guard.human_size(2_621_440), "2.5 MiB")
        self.assertEqual(request_guard.human_size(1_048_576), "1 MiB")
        self.assertEqual(request_guard.human_size(2048), "2 KiB")
        self.assertEqual(request_guard.human_size(500), "500 bytes")


class TestChunked(Guard):
    """A body with no `Content-Length`. Django's WSGI request reads only as many bytes as
    `Content-Length` declares, so a chunked body arrives EMPTY: measured through gunicorn, a
    chunked `ping` with an id came back with no id. Nothing is buffered, so there is no size to
    enforce, but the call must not be silently dropped (an authenticated one answered 202)."""

    chunked = {"CONTENT_LENGTH": "", "HTTP_TRANSFER_ENCODING": "chunked"}

    def test_a_chunked_body_is_refused_with_a_reason(self):
        self._key()
        res = self.post_raw(_ping_body(), **self.chunked)
        self.assertEqual(res.status_code, 411)
        self.assertEqual(
            res.json(),
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "The request body has no Content-Length (it was sent chunked). "
                    "Send the whole request as one body with a Content-Length header.",
                },
            },
        )

    def test_a_streamed_body_above_the_limit_is_refused_and_never_read(self):
        self._key()
        with mock.patch(
            "django.core.handlers.wsgi.WSGIRequest.read", side_effect=AssertionError("body read")
        ):
            res = self.post_raw(_ping_body(LIMIT * 3), **self.chunked)
        self.assertEqual(res.status_code, 411)

    def test_refused_before_authentication_and_without_a_traceback(self):
        with self.assertNoLogs(level="ERROR"):
            with self.assertNumQueries(0):
                res = self.post_raw(_ping_body(LIMIT * 3), **self.chunked)
        self.assertEqual(res.status_code, 411)

    def test_a_declared_length_wins_over_a_transfer_encoding_header(self):
        self._key()
        res = self.post_raw(_ping_body(), HTTP_TRANSFER_ENCODING="chunked")
        self.assertEqual(res.status_code, 200)

    def test_an_empty_post_without_either_header_is_as_before(self):
        self._key()
        res = self.post_raw(b"", CONTENT_LENGTH="")
        self.assertEqual(res.status_code, 202)


class TestIdEcho(Guard):
    """The id echoed in a 401 / 503 used to cost a parse of the whole body."""

    def test_a_small_unauthenticated_body_still_echoes_its_id(self):
        res = self.post_raw(_ping_body(10_000, rid=42))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["id"], 42)

    def test_a_body_at_the_echo_limit_still_echoes(self):
        res = self.post_raw(_ping_body(request_guard.ECHO_ID_MAX_BYTES, rid=42))
        self.assertEqual(res.json()["id"], 42)

    def test_a_larger_unauthenticated_body_is_not_parsed_for_its_id(self):
        spy = self.parse_spy()
        res = self.post_raw(_ping_body(request_guard.ECHO_ID_MAX_BYTES + 1, rid=42))
        spy.assert_not_called()
        self.assertEqual(res.status_code, 401)
        self.assertIsNone(res.json()["id"])
        self.assertEqual(res.json()["error"]["code"], -32600)

    def test_a_megabyte_unauthenticated_is_a_401_that_never_parsed(self):
        spy = self.parse_spy()
        res = self.post_raw(_ping_body(1_000_000))
        spy.assert_not_called()
        self.assertEqual(res.status_code, 401)

    def test_the_switched_off_answer_follows_the_same_rule(self):
        with override_settings(MCP_DISABLED=True):
            small = self.post_raw(_ping_body(1000, rid=9))
            spy = self.parse_spy()
            big = self.post_raw(_ping_body(100_000, rid=9))
            spy.assert_not_called()
        self.assertEqual((small.status_code, small.json()["id"]), (503, 9))
        self.assertEqual((big.status_code, big.json()["id"]), (503, None))

    def test_a_large_authenticated_body_below_the_limit_is_parsed_as_ever(self):
        self._key()
        res = self.post_raw(_ping_body(1_000_000, rid=5))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b'{"jsonrpc":"2.0","id":5,"result":{}}')


class TestUnchanged(Guard):
    """Twin of `main`: these bytes were captured from `main` and must stay as they are."""

    def test_ping(self):
        self._key()
        res = self._rpc("ping")
        self.assertEqual(
            (res.status_code, res.content), (200, b'{"jsonrpc":"2.0","id":1,"result":{}}')
        )

    def test_a_notification_is_202_and_empty(self):
        self._key()
        res = self._rpc("notifications/initialized", rid=None)
        self.assertEqual((res.status_code, res.content), (202, b""))

    def test_no_credential_401(self):
        res = self.post_raw(_ping_body(rid=7))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":7,"error":{"code":-32600,"message":"Authentication credentials were not provided."}}',
        )
        self.assertEqual(res["WWW-Authenticate"], 'Bearer realm="api"')

    def test_a_refused_key_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="ApiKey gnos_nope")
        res = self.post_raw(_ping_body(rid=7))
        self.assertEqual(res.status_code, 401)
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":7,"error":{"code":-32600,"message":"Invalid or revoked API key."}}',
        )

    def test_invalid_json_is_the_same_400(self):
        self._key()
        res = self.post_raw(b"{not json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.content,
            b'{"detail":"JSON parse error - Expecting property name enclosed in double quotes: '
            b'line 1 column 2 (char 1)"}',
        )

    def test_a_batch_is_the_same_400(self):
        self._key()
        res = self.post_raw(b"[1]")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Expected a single JSON-RPC request object."}}',
        )

    def test_the_switched_off_answer(self):
        with override_settings(MCP_DISABLED=True):
            res = self.post_raw(_ping_body(rid=3))
        self.assertEqual(res.status_code, 503)
        self.assertEqual(
            res.content,
            b'{"jsonrpc":"2.0","id":3,"error":{"code":-32001,"message":"MCP is temporarily unavailable."}}',
        )

    def test_tools_list_and_a_tool_call_are_unchanged(self):
        self._key()
        listed = self._rpc("tools/list")
        self.assertEqual(listed.status_code, 200)
        self.assertGreater(len(listed.content), 10_000)
        called = self._call("get_current_user")
        self.assertEqual(called.status_code, 200)
        self.assertFalse(called.json()["result"]["isError"])
        self.assertTrue(MCP)

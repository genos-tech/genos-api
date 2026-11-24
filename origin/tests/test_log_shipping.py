"""The external log archive handler (MIP-2261): off by default, redacted, never blocks.

SimpleTestCase, no DB, no network (the opener is mocked).
"""

import json
import logging
import urllib.error
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import log_shipping
from apis.log_shipping import BetterStackHandler, ingest_url, is_enabled

ENV_OK = {
    "BETTERSTACK_SOURCE_TOKEN": "tok_test_123",
    "BETTERSTACK_INGEST_URL": "https://s1.eu-nbg-2.betterstackdata.com/",
}


def _record(msg, level=logging.INFO):
    return logging.LogRecord("origin.t", level, __file__, 1, msg, (), None)


class EnabledTests(SimpleTestCase):
    def test_off_without_env(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(is_enabled())

    def test_needs_both_token_and_url(self):
        with mock.patch.dict("os.environ", {"BETTERSTACK_SOURCE_TOKEN": "t"}, clear=True):
            self.assertFalse(is_enabled())
        url_only = {"BETTERSTACK_INGEST_URL": ENV_OK["BETTERSTACK_INGEST_URL"]}
        with mock.patch.dict("os.environ", url_only, clear=True):
            self.assertFalse(is_enabled())

    def test_on_with_both(self):
        with mock.patch.dict("os.environ", ENV_OK, clear=True):
            self.assertTrue(is_enabled())

    def test_plain_http_or_garbage_url_is_refused(self):
        for bad in ("http://example.com/", "ftp://x/", "not a url", "https://"):
            env = {**ENV_OK, "BETTERSTACK_INGEST_URL": bad}
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertIsNone(ingest_url(), bad)
                self.assertFalse(is_enabled(), bad)

    def test_test_settings_have_no_shipping_handler(self):
        self.assertNotIn("betterstack", settings.LOGGING["handlers"])


class HandlerTests(SimpleTestCase):
    def test_ships_redacted_batch_with_bearer_token(self):
        with (
            mock.patch.dict("os.environ", ENV_OK, clear=True),
            mock.patch("apis.log_shipping.urllib.request.build_opener") as build,
        ):
            handler = BetterStackHandler()
            handler._ensure_thread = lambda: None  # drive it by hand
            handler.emit(_record("login by ken@example.com ok"))
            handler.emit(_record("second", logging.ERROR))
            handler.flush_now()
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer tok_test_123")
            body = json.loads(request.data.decode())
            self.assertEqual(len(body), 2)
            self.assertNotIn("ken@example.com", json.dumps(body))
            self.assertEqual(body[1]["level"], "error")
            self.assertIn("dt", body[0])
            self.assertEqual(handler.sent, 2)

    def test_full_queue_drops_and_never_raises(self):
        with mock.patch.dict("os.environ", ENV_OK, clear=True):
            handler = BetterStackHandler()
            handler._ensure_thread = lambda: None
            handler._queue = log_shipping.queue.Queue(maxsize=1)
            handler.emit(_record("a"))
            handler.emit(_record("b"))
            self.assertEqual(handler.dropped, 1)

    def test_network_failure_is_swallowed(self):
        with (
            mock.patch.dict("os.environ", ENV_OK, clear=True),
            mock.patch("apis.log_shipping.urllib.request.build_opener") as build,
        ):
            build.return_value.open.side_effect = urllib.error.URLError("down")
            handler = BetterStackHandler()
            handler._ensure_thread = lambda: None
            handler.emit(_record("x"))
            handler.flush_now()
            self.assertEqual(handler.send_failures, 1)
            self.assertEqual(handler.sent, 0)

    def test_nothing_sent_when_env_removed(self):
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("apis.log_shipping.urllib.request.build_opener") as build,
        ):
            handler = BetterStackHandler()
            handler._ensure_thread = lambda: None
            handler.emit(_record("x"))
            handler.flush_now()
            build.return_value.open.assert_not_called()

    def test_redirects_are_refused(self):
        handler = log_shipping._NoRedirect()
        result = handler.redirect_request(None, None, 302, "Found", {}, "https://evil/")
        self.assertIsNone(result)

    def test_emit_starts_one_worker_thread(self):
        with mock.patch.dict("os.environ", ENV_OK, clear=True):
            handler = BetterStackHandler()
            with mock.patch("apis.log_shipping.threading.Thread") as thread:
                handler.emit(_record("a"))
                handler.emit(_record("b"))
                self.assertEqual(thread.call_count, 1)
                thread.return_value.start.assert_called_once()

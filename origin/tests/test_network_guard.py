"""The network guard (MIP-1776, `origin/tests/network_guard.py`) refuses real connections by name.

No database, and no real connection: every address used here is refused before `connect` runs,
except the loopback listener, which is local.
"""

import socket
import unittest

from django.test import SimpleTestCase

from origin.tests import network_guard as guard


def run_nested(case_class, name):
    result = unittest.TestResult()
    unittest.TestSuite([case_class(name)]).run(result)
    return result


class GuardTests(SimpleTestCase):
    def test_the_hook_is_installed(self):
        self.assertTrue(SimpleTestCase._network_guard)
        guard.install()  # a second call is harmless

    def test_a_public_address_is_refused_with_the_test_id_and_the_address(self):
        with self.assertRaises(guard.NetworkGuardError) as cm:
            socket.create_connection(("192.0.2.1", 443), timeout=1)
        self.assertIn(self.id(), str(cm.exception))
        self.assertIn("192.0.2.1:443", str(cm.exception))
        guard._violations.clear()  # expected here: do not fail this test at its end

    def test_the_refusal_shows_the_host_name_the_code_asked_for(self):
        guard._names["192.0.2.7"] = "api.openai.com"
        try:
            with self.assertRaises(guard.NetworkGuardError) as cm:
                socket.socket().connect(("192.0.2.7", 443))
            self.assertIn("api.openai.com:443", str(cm.exception))
        finally:
            del guard._names["192.0.2.7"]
            guard._violations.clear()

    def test_connect_ex_is_guarded_too(self):
        with self.assertRaises(guard.NetworkGuardError):
            socket.socket().connect_ex(("192.0.2.1", 80))
        guard._violations.clear()

    def test_loopback_is_allowed(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2):
                pass

    def test_a_configured_dev_stack_host_is_allowed(self):
        old = guard._allowed_ips
        guard._allowed_ips = {"192.0.2.9"}
        try:
            self.assertTrue(guard._is_allowed("192.0.2.9"))
            self.assertFalse(guard._is_allowed("192.0.2.10"))
            self.assertFalse(guard._is_allowed("not-an-ip"))
        finally:
            guard._allowed_ips = old

    def test_a_refusal_swallowed_by_the_code_still_fails_the_test(self):
        class Swallower(SimpleTestCase):
            def test_swallows(self):
                try:
                    socket.create_connection(("192.0.2.1", 443), timeout=1)
                except Exception:  # noqa: BLE001 - the case the guard must survive
                    pass

        result = run_nested(Swallower, "test_swallows")
        self.assertEqual(len(result.failures), 1, result.failures + result.errors)
        test, message = result.failures[0]
        self.assertIn("Swallower.test_swallows", message)
        self.assertIn("192.0.2.1:443", message)

    def test_a_test_that_stays_off_the_network_passes(self):
        class Quiet(SimpleTestCase):
            def test_quiet(self):
                pass

        result = run_nested(Quiet, "test_quiet")
        self.assertTrue(result.wasSuccessful())

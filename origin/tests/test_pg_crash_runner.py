"""MIP-1633: the test runner stops at the first Postgres crash/restart error.

A real restart cost a full run 1,486 misleading `setUpClass` errors. The runner must (a) recognise
the server's restart messages, (b) stop the run and say so, and (c) leave every other error alone.
"""

import io
import sys
import unittest

from django.db.utils import IntegrityError, OperationalError
from django.test import SimpleTestCase

from apis.test_runner import (
    PostgresCrashAwareResult,
    PostgresCrashAwareRunner,
    postgres_restart_reason,
)

RECOVERY = (
    'connection to server at "pg" (172.19.0.2), port 5432 failed: '
    "FATAL:  the database system is in recovery mode\n"
)


def exc_info(exc):
    try:
        raise exc
    except type(exc):
        return sys.exc_info()


class RestartReasonTests(SimpleTestCase):
    def test_recovery_mode_is_a_restart(self):
        self.assertIn(
            "in recovery mode", postgres_restart_reason(exc_info(OperationalError(RECOVERY)))
        )

    def test_other_restart_messages(self):
        for text in (
            "FATAL:  the database system is not yet accepting connections",
            "FATAL:  the database system is starting up",
            "server closed the connection unexpectedly\n\tThis probably means the server terminated abnormally",
            "WARNING:  terminating connection because of crash of another server process",
            "FATAL:  the database system is shutting down",
            'connection to server at "pg" (172.19.0.2), port 5432 failed: Connection refused',
        ):
            with self.subTest(text=text):
                self.assertTrue(postgres_restart_reason(exc_info(OperationalError(text))))

    def test_other_errors_are_not_a_restart(self):
        # A plain OperationalError (deadlock, timeout) and other exception types are ordinary
        # failures.
        for exc in (
            OperationalError("deadlock detected"),
            IntegrityError("in recovery mode"),
            ValueError("in recovery mode"),
        ):
            with self.subTest(exc=exc):
                self.assertEqual("", postgres_restart_reason(exc_info(exc)))

    def test_no_exception(self):
        self.assertEqual("", postgres_restart_reason((None, None, None)))
        self.assertEqual("", postgres_restart_reason(None))


class _Broken(unittest.TestCase):
    outcome = None

    def test_x(self):
        pass


class ResultTests(SimpleTestCase):
    def make_result(self):
        stream = unittest.runner._WritelnDecorator(io.StringIO())
        return PostgresCrashAwareResult(stream, descriptions=True, verbosity=1), stream

    def test_first_restart_error_stops_the_run_and_says_so(self):
        result, stream = self.make_result()
        test = _Broken("test_x")
        result.addError(test, exc_info(OperationalError(RECOVERY)))
        self.assertTrue(result.shouldStop)
        self.assertIn("test_x", result.postgres_crash[0])
        self.assertIn("POSTGRES CRASHED, RESTARTED OR WENT AWAY", stream.getvalue())
        self.assertIn("in recovery mode", stream.getvalue())
        self.assertEqual(1, len(result.errors))

    def test_ordinary_errors_do_not_stop_the_run(self):
        result, stream = self.make_result()
        result.addError(_Broken("test_x"), exc_info(ValueError("boom")))
        result.addError(_Broken("test_x"), exc_info(OperationalError("deadlock detected")))
        self.assertFalse(result.shouldStop)
        self.assertIsNone(result.postgres_crash)
        self.assertNotIn("POSTGRES CRASHED", stream.getvalue())

    def test_banner_is_printed_once(self):
        result, stream = self.make_result()
        for _ in range(3):
            result.addError(_Broken("test_x"), exc_info(OperationalError(RECOVERY)))
        self.assertEqual(1, stream.getvalue().count("POSTGRES CRASHED, RESTARTED OR WENT AWAY"))


class RunnerWiringTests(SimpleTestCase):
    def test_result_class_is_crash_aware(self):
        runner = PostgresCrashAwareRunner()
        self.assertTrue(issubclass(runner.get_resultclass(), PostgresCrashAwareResult))

    def test_debug_sql_result_class_keeps_its_behaviour(self):
        from django.test.runner import DebugSQLTextTestResult

        runner = PostgresCrashAwareRunner(debug_sql=True)
        cls = runner.get_resultclass()
        self.assertTrue(issubclass(cls, DebugSQLTextTestResult))
        self.assertTrue(hasattr(cls, "postgres_crash"))

    def test_settings_test_selects_the_runner(self):
        from django.conf import settings

        self.assertEqual("apis.test_runner.PostgresCrashAwareRunner", settings.TEST_RUNNER)

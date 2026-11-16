"""Test runner that stops a run as soon as Postgres has crashed or restarted (MIP-1633).

When the Postgres server restarts mid-run, every later test class fails in `setUpClass` with
`OperationalError: the database system is in recovery mode` (1,486 of them in one 3,333 s run on
2026-11-16), which reads like 1,486 broken tests instead of one infrastructure event. This runner
recognises the first such error, says so plainly, and stops the run: nothing after that point is
a result.

Wired in through `TEST_RUNNER` in `apis.settings_test`. It only looks at errors that reach the
result, so a test that deliberately provokes and catches an OperationalError is not affected.
"""

import sys
import unittest

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.utils import OperationalError
from django.test.runner import DiscoverRunner

# What a psycopg2 OperationalError says while the server is restarting after a backend crash, right
# after the connection was cut by it, or once the server is gone. They only count as an abort when
# they reach a test result: the database was reachable when the run started (the test database
# is created first), so a refused connection mid-run means it went away.
POSTGRES_RESTART_MARKERS = (
    "in recovery mode",
    "not yet accepting connections",
    "the database system is starting up",
    "the database system is shutting down",
    "crash of another server process",
    "server closed the connection unexpectedly",
    "Connection refused",
)


def postgres_restart_reason(exc_info):
    """The server's own words if `exc_info` (a sys.exc_info() tuple) is a Postgres restart, else ''."""
    exc = exc_info[1] if exc_info else None
    if not isinstance(exc, OperationalError):
        return ""
    text = str(exc)
    if any(marker in text for marker in POSTGRES_RESTART_MARKERS):
        return " ".join(text.split())
    return ""


def crash_banner(test, reason):
    return (
        "\n"
        "!! POSTGRES CRASHED, RESTARTED OR WENT AWAY during this run, first seen in "
        f"{test}.\n"
        f"!! Server said: {reason}\n"
        "!! Stopping now: every result after this point is invalid (the next test classes would "
        "all fail in setUpClass). Check `docker logs genos-pg` for 'terminating any other active "
        "server processes', then re-run.\n"
    )


class PostgresCrashMixin:
    postgres_crash = None  # (test description, server message) of the first restart error

    def addError(self, test, err):
        super().addError(test, err)
        self._check_postgres_restart(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._check_postgres_restart(subtest, err)

    def _check_postgres_restart(self, test, err):
        if self.postgres_crash is not None:
            return
        reason = postgres_restart_reason(err)
        if reason:
            self.postgres_crash = (str(test), reason)
            self.stream.writeln(crash_banner(*self.postgres_crash))
            self.stop()


class PostgresCrashAwareResult(PostgresCrashMixin, unittest.TextTestResult):
    pass


class PostgresCrashAwareRunner(DiscoverRunner):
    postgres_crash = None  # set by run_suite when the run was cut short by a restart

    def get_resultclass(self):
        base = super().get_resultclass()  # DebugSQLTextTestResult / PDBDebugResult, or None
        if base is None:
            return PostgresCrashAwareResult
        return type(base.__name__, (PostgresCrashMixin, base), {})

    def run_suite(self, suite, **kwargs):
        result = super().run_suite(suite, **kwargs)
        crash = self.postgres_crash = getattr(result, "postgres_crash", None)
        if crash:
            # Last thing on screen, after the summary and the error list.
            print(crash_banner(*crash), file=sys.stderr)
        return result

    def teardown_databases(self, old_config, **kwargs):
        try:
            super().teardown_databases(old_config, **kwargs)
        except OperationalError:
            if not self.postgres_crash:
                raise
            # The server is still recovering, so the DROP cannot run; the banner above says why.
            name = connections[DEFAULT_DB_ALIAS].settings_dict["NAME"]
            print(
                f"!! Could not drop the test database {name} (Postgres is down); it is left "
                "behind, drop it by hand before the next run with the same suffix.",
                file=sys.stderr,
            )

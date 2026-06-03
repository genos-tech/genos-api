"""Tests for the CronCommand fail-loud base (origin/management/cron_command.py).

A scheduled command must exit non-zero when something fails, so the cron
runner marks the run failed instead of green. CronCommand enforces that by
tripping on any ERROR logged under the `origin` logger during the run.
"""

import logging

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from origin.management.cron_command import CronCommand

# BaseCommand.execute() reads these option keys directly.
_EXEC_OPTS = {"force_color": False, "no_color": False, "skip_checks": True}


class _LogsAppError(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.cron").error("something failed")


class _Clean(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.cron").info("all good")


class _LogsForeignError(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        # Outside the `origin` namespace — must NOT trip the wire.
        logging.getLogger("somethirdparty").error("not our problem")


class _Raises(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        raise CommandError("explicit failure")


class TestCronCommand(SimpleTestCase):
    def test_error_log_fails_the_run(self):
        with self.assertRaises(CommandError):
            _LogsAppError().execute(**_EXEC_OPTS)

    def test_clean_run_succeeds(self):
        # No ERROR logged → no raise.
        _Clean().execute(**_EXEC_OPTS)

    def test_foreign_logger_error_is_ignored(self):
        # ERROR outside `origin.*` doesn't red the run.
        _LogsForeignError().execute(**_EXEC_OPTS)

    def test_explicit_raise_propagates(self):
        with self.assertRaises(CommandError):
            _Raises().execute(**_EXEC_OPTS)

    def test_tripwire_detached_after_run(self):
        # The handler must be removed so it doesn't leak across runs.
        before = list(logging.getLogger("origin").handlers)
        _Clean().execute(**_EXEC_OPTS)
        after = list(logging.getLogger("origin").handlers)
        self.assertEqual(before, after)

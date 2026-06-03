"""Shared base for scheduled (cron) management commands.

Railway — and most cron runners — mark a run **failed only on a non-zero
process exit code**. A plain `BaseCommand` that catches its own errors and
logs/prints them still exits 0, so a cron that failed every operation
shows up green and the failure is invisible. (This bit us: during an
OpenSearch outage `opensearch_reindex` logged `success=0` every tick but
the cron stayed green.)

`CronCommand` makes such jobs fail LOUD. For the duration of the run it
attaches a tripwire to the app's top-level ``origin`` logger; if anything
is logged at ``ERROR`` (or the command raises), the command exits non-zero
so the run is marked failed.

Usage: subclass `CronCommand` instead of `BaseCommand`, and log real
failures at ERROR (``log.error(...)`` / ``log.exception(...)``) under an
``origin.*`` logger. Expected/benign conditions should stay at WARNING or
below so they don't trip the wire.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

# Only watch the app's own loggers — third-party libraries occasionally
# log at ERROR for conditions we tolerate, and we don't want those to red
# a cron run.
WATCHED_LOGGER = "origin"


class _ErrorTripwire(logging.Handler):
    """Counts ERROR (and above) records emitted while attached."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0
        self.first_message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        self.count += 1
        if self.first_message is None:
            try:
                self.first_message = record.getMessage()
            except Exception:  # noqa: BLE001 — logging must never break the run
                self.first_message = str(record.msg)


class CronCommand(BaseCommand):
    """`BaseCommand` that exits non-zero if any ERROR is logged during the run.

    Subclasses implement `handle()` exactly as usual; this base wraps
    `execute()` to install/remove the tripwire and raise `CommandError`
    afterwards when errors were seen.
    """

    def execute(self, *args, **options):
        tripwire = _ErrorTripwire()
        watched = logging.getLogger(WATCHED_LOGGER)
        watched.addHandler(tripwire)
        try:
            result = super().execute(*args, **options)
        finally:
            watched.removeHandler(tripwire)
        if tripwire.count:
            raise CommandError(
                f"Run logged {tripwire.count} error(s) "
                f"(first: {tripwire.first_message}). Failing so the cron run "
                "is marked failed."
            )
        return result

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

It also owns `drain_passes()` — the answer to a scheduler that cannot be
asked to run every minute. See that method.

**Heartbeats (MIP-772).** A non-zero exit marks the run red in Railway,
but nobody watches that page, and a cron that stopped running at all
shows nothing red anywhere. So a command started with `--heartbeat
<service>` also records each run in `CronHeartbeat`: started, then
succeeded or failed. A failure opens an alert, and `ops_alert_tick`
notices one that has gone quiet. The flag goes in the railway-*.toml
start command and nowhere else, so a run started by hand or in-process
(the demo seeder runs `opensearch_reindex`) never stands in for the
cron. `HeartbeatCommand` gives it to a command that isn't a
`CronCommand`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apis.log_redaction import redact
from apis.request_id import describe_exception

# Only watch the app's own loggers — third-party libraries occasionally
# log at ERROR for conditions we tolerate, and we don't want those to red
# a cron run.
WATCHED_LOGGER = "origin"

# Seconds between passes when draining. One minute, because that is the
# resolution the jobs using this actually want.
DEFAULT_DRAIN_INTERVAL = 60


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


def _failure_label(exc: BaseException) -> str:
    """What a failed run's heartbeat keeps: the exception type, plus the
    message only for our own `CommandError`, which is written to be read
    (and is redacted here anyway). Anything else's text can quote data."""
    if isinstance(exc, CommandError):
        return redact(f"CommandError: {exc}")
    return describe_exception(exc)


class HeartbeatCommand(BaseCommand):
    """`BaseCommand` with the `--heartbeat <service>` flag: records each run
    in `CronHeartbeat` when given it, and nothing otherwise."""

    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.add_argument(
            "--heartbeat",
            default="",
            metavar="SERVICE",
            help=(
                "Record this run as the cron SERVICE's heartbeat, so a failed "
                "or missing run raises an alert. Set by railway-*.toml only."
            ),
        )
        return parser

    def execute(self, *args, **options):
        name = options.get("heartbeat") or ""
        if not name:
            return self._execute(*args, **options)
        # Imported here: management commands load before the app registry
        # is ready to import models at module level.
        from origin.services import ops_alerts  # noqa: PLC0415

        ops_alerts.cron_started(name)
        try:
            result = self._execute(*args, **options)
        except BaseException as exc:
            ops_alerts.cron_failed(name, _failure_label(exc))
            raise
        ops_alerts.cron_succeeded(name)
        return result

    def _execute(self, *args, **options):
        """The run itself. The heartbeat wraps it, so whatever a subclass
        does here to decide that a run failed is recorded as a failure."""
        return super().execute(*args, **options)


class CronCommand(HeartbeatCommand):
    """`BaseCommand` that exits non-zero if any ERROR is logged during the run.

    Subclasses implement `handle()` exactly as usual; this base wraps
    `execute()` to install/remove the tripwire and raise `CommandError`
    afterwards when errors were seen.
    """

    def create_parser(self, prog_name, subcommand, **kwargs):
        """Add the drain options to EVERY cron command, not one at a time.

        Put here rather than in each `add_arguments` because the reason
        they exist is a property of the runner, not of any one job: a
        second command needing minutely work must not have to rediscover
        that Railway won't schedule it.
        """
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.add_argument(
            "--drain-seconds",
            type=int,
            default=0,
            help=(
                "Keep making passes for up to this many seconds, one every "
                "--drain-interval, then exit. 0 (the default) means a single "
                "pass, which is what a human running this by hand wants."
            ),
        )
        parser.add_argument(
            "--drain-interval",
            type=int,
            default=DEFAULT_DRAIN_INTERVAL,
            help="Seconds between passes while draining.",
        )
        return parser

    def drain_passes(self, **options) -> Iterator[int]:
        """Yield 1, 2, 3… sleeping between passes until the budget is spent.

        **Why this exists.** Railway's cron cannot run a job more often
        than every 5 minutes — "the shortest time between successive
        executions of a cron job cannot be less than 5 minutes", and their
        own docs list more-frequent-than-5-minutes under *when not to use
        cron*. Two jobs here nevertheless want minutely resolution, and
        asking for `* * * * *` anyway does not degrade gracefully: the
        schedule simply does not run (that is how to-do reminders shipped
        needing a manual poke in the Railway UI after every deploy).

        So the cron fires at a rate Railway supports and the *command*
        supplies the resolution, by making several passes inside one run.

        **The budget must stay under the cron interval.** Railway does not
        terminate deployments and skips a scheduled run whose predecessor
        is still alive, so a drain that overran its window would not be
        merely late — it would cancel the next tick and halve the rate,
        which is exactly the failure this is fixing. Hence: never sleep
        into the deadline, and stop early if a pass itself overran.
        """
        seconds = max(0, options.get("drain_seconds") or 0)
        interval = max(1, options.get("drain_interval") or DEFAULT_DRAIN_INTERVAL)
        deadline = time.monotonic() + seconds
        number = 0

        while True:
            number += 1
            started = time.monotonic()
            yield number

            next_start = started + interval
            # `next_start` covers the common case; the `time.monotonic()`
            # arm catches a pass that took longer than the whole interval,
            # where the next one would begin already past the window.
            if next_start >= deadline or time.monotonic() >= deadline:
                return
            time.sleep(max(0.0, next_start - time.monotonic()))
            # We just held a connection idle for a minute, which is long
            # enough for the far end (or a pooler) to have hung up on it.
            # Django does this at request boundaries for the same reason;
            # a pass is this job's equivalent of a request.
            close_old_connections()

    def _execute(self, *args, **options):
        tripwire = _ErrorTripwire()
        watched = logging.getLogger(WATCHED_LOGGER)
        watched.addHandler(tripwire)
        try:
            result = super()._execute(*args, **options)
        finally:
            watched.removeHandler(tripwire)
        if tripwire.count:
            # Printed to stderr by Django, outside the logging formatters
            # that would otherwise redact it.
            raise CommandError(
                f"Run logged {tripwire.count} error(s) "
                f"(first: {redact(tripwire.first_message or '')}). Failing so "
                "the cron run is marked failed."
            )
        return result

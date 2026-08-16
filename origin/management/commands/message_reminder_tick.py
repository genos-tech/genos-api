"""Fire reminders that have come due — messages AND to-dos.

Makes a pass every minute. Each pass takes the pending reminders whose
`remind_at` has passed, files an Inbox item in the Activities section for
each, and web-pushes the person who asked for it.

⚠️ **The cron does not run every minute — this command does.** Railway
refuses to schedule anything more often than every 5 minutes, so the
service fires `*/5` and passes `--drain-seconds`, and `drain_passes()`
makes the four one-minute passes inside that single run. The minutely
resolution is real; the *scheduling* of it is not the cron's. Asking
Railway for `* * * * *` anyway is not a degraded schedule but a dead one —
see `CronCommand.drain_passes`, and `railway-reminder-tick.toml`.

Both kinds drain in one pass, and the command keeps its message-era name so
the Railway cron service that invokes it needs no change. That is the point:
every half of a reminder's delivery is this job's work, so a second cron
service would be a second way for a whole feature to fail silently — which
is exactly how message reminders shipped broken the first time. One service
to exist means one service to notice.

Modelled on `webhook_deliver_tick`, the proven outbox in this repo, minus
the retry machinery: there is nobody else's server to be unavailable here,
so a reminder either fires or the transaction rolls back and the next tick
picks it up again. `fired_at` doubles as the claim, so overlapping passes
cannot deliver the same reminder twice (see
`services/message_reminders.fire`).

⚠️ **Being a minute late is normal; being an hour late is a bug.** Minutely
passes are what make "remind me in 20 minutes" mean anything, so a backlog
is the signal worth watching — hence the `late=` figure in the summary line,
which now carries a `[pass N]` marker while draining so a wedged pass is
visible as a run that stopped counting. Individual failures log at ERROR
through the service's `logger.exception`, which `CronCommand` turns into a
failed run.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.services import todo_reminders
from origin.services.message_reminders import due_reminders, fire_due

log = logging.getLogger("origin.reminders")

DEFAULT_LIMIT = 500
# A reminder still unfired this long after its time means the cron was down
# (or a pass is wedged). Worth a WARNING so it shows up before users report
# reminders that never came.
LATE_MINUTES = 10


class Command(CronCommand):
    help = "Deliver message and to-do reminders whose time has come."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help="Max reminders per pass. A brake, not a scheduler.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fire nothing; just report what is due.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]

        if options["dry_run"]:
            # One report, never a drain: "what is due" is a question, and
            # asking it four times over four minutes answers a different one.
            now = timezone.now()
            due = due_reminders(now=now, limit=limit)
            todo_due = todo_reminders.due_reminders(now=now, limit=limit)
            self.stdout.write(f"{len(due)} message reminder(s) due")
            self.stdout.write(f"{len(todo_due)} to-do reminder(s) due")
            return

        # The marker is empty for a single pass, so a hand-run keeps the
        # exact output the runbook tells people to read.
        draining = (options.get("drain_seconds") or 0) > 0
        for number in self.drain_passes(**options):
            self._fire_pass(limit=limit, marker=f" [pass {number}]" if draining else "")

    def _fire_pass(self, *, limit: int, marker: str) -> None:
        # `now` is per pass, not per run: a four-minute drain whose passes
        # all shared one timestamp would keep re-asking for the same due
        # set and never see the reminders that came due while it ran.
        now = timezone.now()

        # Each kind gets its own `--limit` rather than sharing one budget: a
        # backlog of one kind must not be able to starve the other, since
        # they belong to different people and both are late by definition.
        # A raised exception from either would fail the run, so they are
        # reported together and the second still runs — the services already
        # swallow per-row failures.
        counts = fire_due(now=now, limit=limit)
        todo_counts = todo_reminders.fire_due(now=now, limit=limit)
        late_seconds = max(counts["max_late_seconds"], todo_counts["max_late_seconds"])

        if late_seconds > LATE_MINUTES * 60:
            log.warning(
                "message_reminder: oldest due reminder was %s minutes late "
                "(cron down, or a pass wedged?)",
                late_seconds // 60,
            )
        self.stdout.write(
            f"message_reminder: {counts['fired']} fired, {counts['skipped']} skipped "
            f"(of {counts['due']} due, oldest {counts['max_late_seconds']}s late){marker}"
        )
        self.stdout.write(
            f"todo_reminder: {todo_counts['fired']} fired, {todo_counts['skipped']} skipped "
            f"(of {todo_counts['due']} due, oldest {todo_counts['max_late_seconds']}s late){marker}"
        )

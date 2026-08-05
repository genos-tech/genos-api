"""Fire message reminders that have come due.

Runs every minute. Each pass takes the pending reminders whose `remind_at`
has passed, files an Inbox item in the Activities section for each, and
web-pushes the person who asked for it.

Modelled on `webhook_deliver_tick`, the proven outbox in this repo, minus
the retry machinery: there is nobody else's server to be unavailable here,
so a reminder either fires or the transaction rolls back and the next tick
picks it up again. `fired_at` doubles as the claim, so overlapping passes
cannot deliver the same reminder twice (see
`services/message_reminders.fire`).

⚠️ **Being a minute late is normal; being an hour late is a bug.** A
minutely cron is what makes "remind me in 20 minutes" mean anything, so a
backlog is the signal worth watching — hence the `late=` figure in the
summary line. Individual failures log at ERROR through the service's
`logger.exception`, which `CronCommand` turns into a failed run.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.services.message_reminders import due_reminders, fire_due

log = logging.getLogger("origin.reminders")

DEFAULT_LIMIT = 500
# A reminder still unfired this long after its time means the cron was down
# (or a pass is wedged). Worth a WARNING so it shows up before users report
# reminders that never came.
LATE_MINUTES = 10


class Command(CronCommand):
    help = "Deliver message reminders whose time has come."

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
        now = timezone.now()
        limit = options["limit"]

        if options["dry_run"]:
            due = due_reminders(now=now, limit=limit)
            self.stdout.write(f"{len(due)} reminder(s) due")
            return

        counts = fire_due(now=now, limit=limit)
        late_seconds = counts["max_late_seconds"]

        if late_seconds > LATE_MINUTES * 60:
            log.warning(
                "message_reminder: oldest due reminder was %s minutes late "
                "(cron down, or a pass wedged?)",
                late_seconds // 60,
            )
        self.stdout.write(
            f"message_reminder: {counts['fired']} fired, {counts['skipped']} skipped "
            f"(of {counts['due']} due, oldest {late_seconds}s late)"
        )

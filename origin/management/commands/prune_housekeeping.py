"""`python manage.py prune_housekeeping` — delete dead credentials, invites and ops rows (MIP-1787).

DRY RUN BY DEFAULT: it prints one count per table and changes nothing. `--apply` deletes, in small
batches. Counts only — never a name, address, token or hash. See
`origin/services/housekeeping_prune.py` for what each set matches and the grace periods.

Usage:
    python manage.py prune_housekeeping                  # counts only
    python manage.py prune_housekeeping --apply
    python manage.py prune_housekeeping --only api_keys --apply
"""

from __future__ import annotations

import logging

from django.core.management.base import CommandError
from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.services import housekeeping_prune as hp

log = logging.getLogger(__name__)


class Command(CronCommand):
    help = "Count (default) or delete expired tokens, revoked keys/connections, done invites, idle push subscriptions, resolved alerts."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete. Without it: counts only.")
        parser.add_argument("--only", action="append", help="Run one set by name (repeatable).")

    def handle(self, *args, **opts):
        names = opts["only"] or [s.name for s in hp.SETS]
        unknown = [n for n in names if n not in hp.BY_NAME]
        if unknown:
            raise CommandError(f"Unknown set {unknown[0]!r}. Known: {', '.join(hp.BY_NAME)}")
        apply = opts["apply"]
        now = timezone.now()
        for s in hp.SETS:
            if s.name not in names:
                continue
            if not apply:
                self.stdout.write(f"[dry-run] {s.name}: would delete {hp.count(s.name, now)}")
                continue
            deleted = hp.delete_in_batches(s.name, now)
            log.info("prune_housekeeping: %s deleted=%d", s.name, deleted)
            self.stdout.write(f"{s.name}: deleted {deleted}")

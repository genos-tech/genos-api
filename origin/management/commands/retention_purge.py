"""`python manage.py retention_purge` - enforce the per-table retention periods (MIP-728).

Every period defaults to "keep forever", so this does nothing until someone sets
`RETENTION_<NAME>_DAYS` for a table. See `origin/services/retention.py` for the
policies, what each one deletes or blanks, and what is deliberately left out.

Run daily via `railway-retention-purge.toml`.

Usage:
    python manage.py retention_purge                  # every policy that has a period
    python manage.py retention_purge --dry-run        # count, change nothing
    python manage.py retention_purge --only agent_runs
    python manage.py retention_purge --list           # the policies and their periods
"""

from __future__ import annotations

import logging

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.services import retention

log = logging.getLogger(__name__)


class Command(CronCommand):
    help = "Delete or blank data older than each table's retention period."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count; change nothing.")
        parser.add_argument("--only", help="Run one policy by name.")
        parser.add_argument("--list", action="store_true", help="Show every policy and exit.")

    def handle(self, *args, **opts):
        if opts["list"]:
            for p in retention.POLICIES:
                days = p.days()
                period = "forever" if days is None else f"{days} days"
                self.stdout.write(f"{p.name:28} {p.action.value:6} {period:10} {p.env_var}")
            return
        try:
            results = retention.run_all(only=opts["only"], dry_run=opts["dry_run"])
        except KeyError as exc:
            names = ", ".join(retention.BY_NAME)
            raise CommandError(f"Unknown policy {exc.args[0]!r}. Known: {names}") from exc

        for r in results:
            if r.skipped:
                self.stdout.write(f"{r.name}: keeping forever (no period set)")
                continue
            verb = "would change" if r.dry_run else "changed"
            line = f"{r.name}: {verb} {r.matched} rows older than {r.days} days"
            self.stdout.write(("[dry-run] " if r.dry_run else "") + line)
            if not r.dry_run:
                log.info("retention_purge: %s changed=%d days=%d", r.name, r.changed, r.days)

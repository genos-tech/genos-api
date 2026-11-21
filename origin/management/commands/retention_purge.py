"""`python manage.py retention_purge` - enforce the per-table retention periods (MIP-728).

Every period defaults to "keep forever", so this changes nothing until someone sets
`RETENTION_<NAME>_DAYS` for a table. **It is a dry run unless `--apply` is given**
(MIP-1786): it prints counts per table, never a title, name or email. See `origin/services/retention.py` for the
policies, what each one deletes or blanks, and what is deliberately left out.

Run daily via `railway-retention-purge.toml`.

Usage:
    python manage.py retention_purge                  # dry run: counts for every policy that has a period
    python manage.py retention_purge --apply          # really delete / blank
    python manage.py retention_purge --approved-periods  # counts for the approved periods (90 d outbox,
                                                      # 180 d agent runs) even with no variable set
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
        parser.add_argument(
            "--apply", action="store_true", help="Delete or blank. Default is a dry run."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Count only (the default; kept for old callers)."
        )
        parser.add_argument(
            "--approved-periods",
            action="store_true",
            help="Dry run with the approved periods (ignores the RETENTION_* variables).",
        )
        parser.add_argument("--only", help="Run one policy by name.")
        parser.add_argument("--list", action="store_true", help="Show every policy and exit.")

    def handle(self, *args, **opts):
        if opts["list"]:
            for p in retention.POLICIES:
                days = p.days()
                period = "forever" if days is None else f"{days} days"
                self.stdout.write(f"{p.name:28} {p.action.value:6} {period:10} {p.env_var}")
            return
        if opts["apply"] and (opts["dry_run"] or opts["approved_periods"]):
            raise CommandError("--apply cannot be combined with --dry-run or --approved-periods.")
        if opts["approved_periods"]:
            return self._approved(opts["only"])
        try:
            results = retention.run_all(only=opts["only"], dry_run=not opts["apply"])
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

    def _approved(self, only):
        if only is not None and only not in retention.BY_NAME:
            raise CommandError(f"Unknown policy {only!r}. Known: {', '.join(retention.BY_NAME)}")
        for p in retention.POLICIES:
            if only and p.name != only:
                continue
            r = retention.preview_approved(p)
            if r is None:
                continue
            self.stdout.write(
                f"[dry-run] {r.name}: would change {r.matched} rows older than {r.days} days"
            )
            if p.name == "agent_runs":
                from origin.search_engine.models import AgentStep

                steps = AgentStep.objects.filter(run__in=p.candidates(days=r.days)).count()
                self.stdout.write(
                    f"[dry-run] agent_runs: {steps} agent steps would go with those runs"
                )

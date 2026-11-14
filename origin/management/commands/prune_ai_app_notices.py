"""`python manage.py prune_ai_app_notices` - expire AI-app notices (MIP-1541).

An AI-app notice (`Activity`, `activity_type` AI_APP) names up to five item titles of the member's
own. The rule (older than `AI_APP_NOTICE_RETENTION_DAYS`, nothing else touched) lives in
`origin/services/ai_app_notice_retention.py`.

    python manage.py prune_ai_app_notices            # DRY RUN: counts only, writes nothing
    python manage.py prune_ai_app_notices --apply    # delete the expired notices

Output is COUNTS ONLY: no title, id, name or e-mail is printed. Safe to re-run. A daily cron runs
it as `python /app/manage.py prune_ai_app_notices --apply --heartbeat <service>` (the dry run is
the default, so a cron that leaves `--apply` out deletes nothing).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import ai_app_notice_retention as retention

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or delete (--apply) AI-app notices older than the retention "
        "window. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the expired notices. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        days = retention.AI_APP_NOTICE_RETENTION_DAYS
        self.stdout.write("prune_ai_app_notices: " + ("APPLY" if apply else "DRY RUN"))
        result = retention.prune(apply=apply)
        if not apply:
            self.stdout.write(
                f"would delete {result.matched} AI-app notices older than {days} days"
            )
            if result.matched:
                self.stdout.write("re-run with --apply to delete them")
            return
        log.info("prune_ai_app_notices: deleted=%d days=%d", result.deleted, days)
        self.stdout.write(f"deleted {result.deleted} AI-app notices older than {days} days")

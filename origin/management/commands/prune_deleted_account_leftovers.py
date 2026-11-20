"""`python manage.py prune_deleted_account_leftovers` - clean what deleted accounts left (MIP-1783).

Account deletion now removes the person's email outbox, inbox, reminders, flags, pins, reactions,
mention rows, recents, to-do schedules, folder favorites, quota counters and feature grants, and
blanks the "who generated this" id on shared summaries (`services/deleted_account_leftovers`).
Accounts deleted BEFORE that left those rows behind (the account is soft-deleted, so no foreign
key cascaded). This finds and removes them: only rows of accounts that are deleted.

    python manage.py prune_deleted_account_leftovers            # DRY RUN: counts only, writes nothing
    python manage.py prune_deleted_account_leftovers --apply    # delete / anonymise them

Output is COUNTS ONLY per table: no title, id, name or e-mail is printed. Safe to re-run;
one-off, not a cron (new deletions clean up after themselves).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import deleted_account_leftovers as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) rows that already-deleted accounts left "
        "behind. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete / anonymise the rows. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_deleted_account_leftovers: " + ("APPLY" if apply else "DRY RUN"))
        result = service.prune(apply=apply)
        tables = ", ".join(f"{name}={n}" for name, n in result.by_table.items()) or "none"
        verb = "changed" if apply else "would delete or anonymise"
        count = result.changed if apply else result.matched
        self.stdout.write(
            f"{verb} {count} rows of {result.accounts} deleted account(s); by table: {tables}"
        )
        if apply:
            log.info("prune_deleted_account_leftovers: changed=%d", result.changed)
        elif result.matched:
            self.stdout.write("re-run with --apply to delete / anonymise them")

"""`python manage.py prune_deleted_account_activity` - clean the feed of deleted accounts (MIP-1542).

Account deletion now deletes every `Activity` row addressed to the account. Accounts deleted
BEFORE that left their feed behind (the account is soft-deleted, so the `recipient` CASCADE never
fired). This finds and removes those rows: only rows whose recipient is a deleted account. Rows
about a deleted account in other people's feeds (it is their `actor`) are never touched.

    python manage.py prune_deleted_account_activity            # DRY RUN: counts only, writes nothing
    python manage.py prune_deleted_account_activity --apply    # delete them

Output is COUNTS ONLY: no title, id, name or e-mail is printed. Safe to re-run; one-off, not a
cron (new deletions clean up after themselves).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import deleted_account_activity as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or delete (--apply) Activity rows addressed to deleted "
        "accounts. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the rows. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_deleted_account_activity: " + ("APPLY" if apply else "DRY RUN"))
        result = service.prune(apply=apply)
        kinds = ", ".join(f"{name}={n}" for name, n in result.by_type.items()) or "none"
        verb = "deleted" if apply else "would delete"
        count = result.deleted if apply else result.matched
        self.stdout.write(
            f"{verb} {count} rows addressed to {result.accounts} deleted account(s); by kind: {kinds}"
        )
        if apply:
            log.info("prune_deleted_account_activity: deleted=%d", result.deleted)
        elif result.matched:
            self.stdout.write("re-run with --apply to delete them")

"""`python manage.py prune_deleted_account_channel_owners` - give chats owned by a deleted account
a live owner (MIP-1550).

Account deletion now hands the group and project chats the account owned to a remaining member
(`services/deleted_account_channel_owners.py` has the rule). Accounts deleted BEFORE that left
their chats pointing at the anonymised account as owner. This finds and fixes those: only chats
whose owner is an already-deleted account, only group and project chats. Direct messages and
multi-person DMs are never touched, and no chat is ever deleted.

    python manage.py prune_deleted_account_channel_owners            # DRY RUN: counts only
    python manage.py prune_deleted_account_channel_owners --apply    # hand them over

Output is COUNTS ONLY: no name, e-mail, title or id. Safe to re-run; one-off, not a cron (new
deletions clean up after themselves).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import deleted_account_channel_owners as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or hand over (--apply) the group and project chats that a "
        "deleted account still owns. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Hand the chats over. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write(
            "prune_deleted_account_channel_owners: " + ("APPLY" if apply else "DRY RUN")
        )
        result = service.prune(apply=apply)
        if not apply:
            self.stdout.write(
                f"{result.chats} group / project chat(s) owned by {result.accounts} deleted "
                "account(s) would be handed over"
            )
            if result.chats:
                self.stdout.write("re-run with --apply to hand them over")
            return
        self.stdout.write(
            f"handed {result.transferred} group(s) to a remaining member, {result.projects_transferred} "
            f"project(s) to the team owner; left {result.ownerless} group(s) ownerless and "
            f"{result.left} project chat(s) unchanged, of {result.accounts} deleted account(s)"
        )
        log.info(
            "prune_deleted_account_channel_owners: groups=%d projects=%d ownerless=%d left=%d",
            result.transferred,
            result.projects_transferred,
            result.ownerless,
            result.left,
        )

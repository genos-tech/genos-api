"""`python manage.py prune_deleted_account_memberships` - take deleted accounts out of group and
project chats (MIP-1546).

Account deletion now ends the account's group (GM) and project (PM) chat memberships. Accounts
deleted BEFORE that left them behind (the account is soft-deleted, so no FK cascade fired). This
finds and ends those: only the rows of accounts that are already deleted, and only group and
project chats. Direct messages and multi-person DMs are never touched (the other participants keep
the conversation).

    python manage.py prune_deleted_account_memberships            # DRY RUN: counts only
    python manage.py prune_deleted_account_memberships --apply    # end them

It ALSO hands the group and project chats such an account still owns to a live owner
(`services/deleted_account_channel_owners.py`, MIP-1550), after the memberships above.

A group row is soft-deleted; a project membership is deleted through the project's own delete, which
soft-deletes the project chat's mirrored row. Output is COUNTS ONLY: no name, e-mail, title or id.
Safe to re-run; one-off, not a cron (new deletions clean up after themselves).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import deleted_account_channel_owners as owners_service
from origin.services import deleted_account_memberships as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or end (--apply) the group and project chat memberships "
        "of deleted accounts. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="End the memberships. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_deleted_account_memberships: " + ("APPLY" if apply else "DRY RUN"))
        result = service.prune(apply=apply)
        verb = "ended" if apply else "would end"
        self.stdout.write(
            f"{verb} {result.group_rows} group rows, {result.project_memberships} project "
            f"memberships and {result.project_chat_rows} project-chat rows without a membership "
            f"of {result.accounts} deleted account(s)"
        )
        if apply:
            log.info(
                "prune_deleted_account_memberships: groups=%d projects=%d stray=%d",
                result.group_rows,
                result.project_memberships,
                result.project_chat_rows,
            )
        elif result.total:
            self.stdout.write("re-run with --apply to end them")

        # Then the chats a deleted account still OWNS (MIP-1550): after the memberships above, so
        # a group is handed to someone who is still in it. Counts only, like everything here.
        owned = owners_service.prune(apply=apply)
        if not apply:
            self.stdout.write(
                f"would hand over {owned.chats} group / project chat(s) owned by {owned.accounts} "
                "deleted account(s) to a live owner (a project may give the team owner a new project seat)"
            )
            return
        self.stdout.write(
            f"handed {owned.transferred} group(s) to a remaining member, "
            f"{owned.projects_transferred} project(s) to the team owner ({owned.seats_added} of them "
            f"gave the team owner a new project seat); left {owned.ownerless} "
            f"group(s) ownerless and {owned.left} project chat(s) unchanged"
        )
        log.info(
            "prune_deleted_account_memberships owners: groups=%d projects=%d seats=%d ownerless=%d left=%d",
            owned.transferred,
            owned.projects_transferred,
            owned.seats_added,
            owned.ownerless,
            owned.left,
        )

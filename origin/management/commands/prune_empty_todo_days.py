"""`python manage.py prune_empty_todo_days` - remove empty to-do days dated after today (MIP-2574).

Until MIP-2574, deleting or moving the last to-do of a day left the day (`ToDoGroup`) behind, and
the app shows a day that exists, so a future list with nothing on it kept appearing. New deletes
and moves remove it (`services/todo_items.delete_day_if_empty`); this removes the ones left over.

    python manage.py prune_empty_todo_days            # DRY RUN: counts only, writes nothing
    python manage.py prune_empty_todo_days --apply    # delete them

Covers every member. "Today" is each member's own (their time zone, else the server's). Today and
past days are left alone. Output is COUNTS ONLY. Safe to re-run; one-off, not a cron.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup
from origin.services import todo_items
from origin.services.user_time import resolve_zone

log = logging.getLogger("origin.todo")


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) empty to-do days dated after their "
        "owner's today. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the days. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_empty_todo_days: " + ("APPLY" if apply else "DRY RUN"))
        now = timezone.now()
        # No zone is more than a day behind UTC, so nothing dated before the UTC date can be
        # after anybody's today.
        rows = ToDoGroup.objects.filter(items__isnull=True, local_date__gte=now.date()).values_list(
            "group_id", "user_id", "local_date", "user__timezone"
        )
        found = [
            (group_id, user_id)
            for group_id, user_id, local_date, zone in rows
            if local_date > now.astimezone(resolve_zone(zone)).date()
        ]
        members = len({user_id for _, user_id in found})
        if not apply:
            self.stdout.write(f"would remove {len(found)} empty days of {members} members")
            if found:
                self.stdout.write("re-run with --apply to remove them")
            return
        # One at a time: each is locked and checked again, so a day that got a to-do since the
        # read above is kept.
        removed = sum(todo_items.delete_day_if_empty(group_id) for group_id, _ in found)
        self.stdout.write(f"removed {removed} of {len(found)} empty days found ({members} members)")
        log.info("prune_empty_todo_days: removed=%d", removed)

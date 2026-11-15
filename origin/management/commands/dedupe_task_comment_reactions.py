"""Remove repeated task comment reactions (MIP-1582).

`POST /api/v2/task/comment/reaction/` had no duplicate guard, so a repeat made a second row for the
same person, emoji and comment (and the DELETE that followed then found two and answered 500). The
route is guarded now; rows already doubled up stay until this runs.

    python manage.py dedupe_task_comment_reactions            # DRY RUN: counts, writes nothing
    python manage.py dedupe_task_comment_reactions --apply    # remove the repeated rows

Per `(task, comment, person, emoji)` the row with the lowest `reaction_id` stays and the later
copies go. A reaction whose person is gone (an account deleted: the column is NULL) is never judged:
those NULLs are different people. Nothing else is touched, nobody is notified (a repeat never told
anyone), and the comment's counter simply has a gap, which nothing reads.

Output is COUNTS ONLY: no task, comment or person id and no emoji. Safe to re-run.

Migration 0246 (MIP-1595) adds the unique index on `(task, comment_id, sender, reaction_emoji)` where
the sender is not NULL, and refuses to run while rows repeat: run this first. After it, a repeat
cannot be written at all, so this command only ever has something to do on a database not yet
migrated.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.task.task_models import TaskCommentReactionFact
from origin.services.task_comment_reactions import repeat_counts, repeated_reaction_rows


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) repeated task comment reactions. "
        "Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Remove the repeated rows. Without it nothing is written.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        self.stdout.write("dedupe_task_comment_reactions: " + ("APPLY" if apply else "DRY RUN"))
        with transaction.atomic():
            counts = repeat_counts()
            removed = 0
            if apply and counts["rows_to_remove"]:
                keys = list(repeated_reaction_rows().values_list("pk", flat=True))
                removed, _ = TaskCommentReactionFact.objects.filter(pk__in=keys).delete()
        self.stdout.write(f"  {counts['rows']} reaction rows scanned")
        self.stdout.write(f"  {counts['groups']} (comment, person, emoji) groups repeat")
        verb = "removed" if apply else "would remove"
        self.stdout.write(f"{verb}: {removed if apply else counts['rows_to_remove']}")
        if not apply and counts["rows_to_remove"]:
            self.stdout.write("re-run with --apply to remove them")

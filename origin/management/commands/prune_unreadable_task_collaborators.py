"""Remove collaborators who cannot open their task (MIP-1579, follow-up of MIP-1567).

MIP-1567 stopped a collaborator who cannot open a task from being added, notified or kept listed
when they leave the project or the task moves. Rows that were already there stay: someone listed
on a task they cannot open (a teammate who was never on its project, or who left it before the
hook existed) is told nothing at send time but is still shown as a collaborator.

    python manage.py prune_unreadable_task_collaborators            # DRY RUN: counts, writes nothing
    python manage.py prune_unreadable_task_collaborators --apply    # remove those collaborator rows

The rule is `can_access_task`'s, in batch form: a collaborator stays when they are a member or the
owner of the task's project, or its assignee or reporter. A milestone's collaborators ride on its
backing task and are judged the same way. Every task is looked at, soft-deleted ones included (the
rule does not care), and a task in no project keeps only its assignee and reporter.

Only the collaborator rows go: no task, project or person is touched, and nobody is notified.
Output is COUNTS ONLY: no task id or title, no person id, name or e-mail. Safe to re-run: the second
`--apply` finds nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.task.task_models import TaskMaster
from origin.services.task_collaborator_revoke import (
    drop_unreadable_collaborators,
    unreadable_collaborator_rows,
)


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) task collaborators who cannot open "
        "their task. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Remove the collaborator rows. Without it nothing is written.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        self.stdout.write(
            "prune_unreadable_task_collaborators: " + ("APPLY" if apply else "DRY RUN")
        )
        through = TaskMaster.collaborators.through.objects
        with transaction.atomic():
            listed = through.count()
            tasks_with_collaborators = through.values("taskmaster_id").distinct().count()
            rows = unreadable_collaborator_rows()
            tasks_affected = len({task_id for _pk, task_id in rows})
            removed = drop_unreadable_collaborators() if apply and rows else 0
        self.stdout.write(
            f"  {listed} collaborator rows on {tasks_with_collaborators} tasks scanned"
        )
        self.stdout.write(f"  {len(rows)} rows on {tasks_affected} tasks cannot open their task")
        verb = "removed" if apply else "would remove"
        self.stdout.write(f"{verb}: {removed if apply else len(rows)}")
        if not apply and rows:
            self.stdout.write("re-run with --apply to remove them")

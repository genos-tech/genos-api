"""One-off backfill for `TaskMaster.root_task_id`.

A pre-existing bug in `set_root_task_id` (post_save signal on
TaskMaster) stamped `root_task_id = task_id` for every newly-created
task, including sub-tasks that had a `parent_task_id`. That left every
sub-task pointing at itself as the chain root, so the task diagram and
any other "rooted-at-chain-top" UI anchored on the leaf and rendered
only that single node.

The signal is now fixed (it walks up `parent_task_id` to find the real
root), but any task created BEFORE the fix still has the wrong value.
Run this command once after deploying the fix:

    python manage.py backfill_root_task_id              # apply to all tasks
    python manage.py backfill_root_task_id --dry-run    # report only, no writes
    python manage.py backfill_root_task_id --team <id>  # scope to one team

The command is idempotent — running it twice produces no further
writes because pass 2 sees the correct values from pass 1.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.task.task_models import TaskMaster

# Cycle guard cap. Real data shouldn't approach this; if a task chain
# is deeper than this the DB has bad data and we want to bail loudly
# rather than loop forever.
_MAX_DEPTH = 64


def _resolve_root(task_id: int, by_id: dict[int, TaskMaster]) -> int:
    """Walk up `parent_task_id` from `task_id` and return the top-most
    ancestor's id. Falls back to `task_id` itself for orphan refs or
    missing parents."""
    current_id = task_id
    visited: set[int] = set()
    for _ in range(_MAX_DEPTH):
        task = by_id.get(current_id)
        if task is None or task.parent_task_id is None:
            return current_id
        if task.parent_task_id in visited:
            # Cycle — bail at the current node.
            return current_id
        visited.add(current_id)
        current_id = task.parent_task_id
    # Hit depth cap — almost certainly bad data. Return whatever we
    # walked up to so the row at least gets *some* sensible root.
    return current_id


class Command(BaseCommand):
    help = "Backfill TaskMaster.root_task_id by walking up parent_task_id chains."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--team",
            type=str,
            default=None,
            help="Limit to one team_id. Default: scan all teams.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        team_filter: str | None = options["team"]

        qs = TaskMaster.objects.all()
        if team_filter:
            qs = qs.filter(team_id=team_filter)

        # Pull everything we need into memory once so chain-walking is
        # O(1) per hop instead of N queries per task. The signal can
        # afford one DB hop per ancestor because it processes one task
        # at a time; this command processes potentially thousands at
        # once, so we batch the read.
        tasks = list(qs.only("task_id", "parent_task_id", "root_task_id"))
        by_id = {t.task_id: t for t in tasks}
        self.stdout.write(f"Loaded {len(tasks)} task(s).")

        to_update: list[TaskMaster] = []
        for task in tasks:
            expected = _resolve_root(task.task_id, by_id)
            if task.root_task_id != expected:
                task.root_task_id = expected
                to_update.append(task)

        if not to_update:
            self.stdout.write(self.style.SUCCESS("Nothing to update — all roots already correct."))
            return

        self.stdout.write(f"{len(to_update)} task(s) have an incorrect root_task_id.")

        if dry_run:
            preview = to_update[:10]
            for t in preview:
                self.stdout.write(
                    f"  task_id={t.task_id} parent={t.parent_task_id} → "
                    f"root would change to {t.root_task_id}"
                )
            if len(to_update) > len(preview):
                self.stdout.write(f"  … and {len(to_update) - len(preview)} more.")
            self.stdout.write(self.style.WARNING("Dry-run — no writes."))
            return

        with transaction.atomic():
            TaskMaster.objects.bulk_update(to_update, ["root_task_id"], batch_size=500)
        self.stdout.write(self.style.SUCCESS(f"Updated root_task_id on {len(to_update)} task(s)."))

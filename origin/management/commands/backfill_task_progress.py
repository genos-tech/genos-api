"""Rebuild `TaskProgressFact` and `TaskMaster.last_progress_at` (MIP-1351).

The live writer (`origin.services.task_progress`) only sees changes made after
it shipped. Run this once after deploying it, to fill in the history from
`TaskActivity`:

    python manage.py backfill_task_progress              # all tasks
    python manage.py backfill_task_progress --dry-run    # report only, no writes
    python manage.py backfill_task_progress --team <id>  # scope to one team

It applies the same rules as the live writer (`counts_as_progress`), so cascade
rows and derived status flips are skipped.

**It uses TODAY's tree.** A task's past parents are not stored anywhere, so an
old change is attributed to the ancestors the task has now. History from before
the writer shipped is therefore approximate wherever a task has been moved;
everything written after is exact.

Idempotent: a fact is unique per (activity, ancestor), so a second run inserts
nothing, and `last_progress_at` is only ever raised, never lowered. A task with
no history at all gets its creation time. `ts_updated_at` is not touched.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.services.task_progress import ancestor_ids, counts_as_progress

_BATCH = 2000


class Command(BaseCommand):
    help = "Rebuild TaskProgressFact and TaskMaster.last_progress_at from TaskActivity."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--team", help="Only this team_id.")

    def handle(self, *args, dry_run=False, team=None, **options):
        tasks = TaskMaster.objects.filter(is_init_task=False)
        if team:
            tasks = tasks.filter(team_id=team)
        project_ids = sorted(
            set(tasks.values_list("project_id", flat=True)), key=lambda p: (p is None, p)
        )
        facts = moved = 0
        for project_id in project_ids:
            f, m = self._project(tasks.filter(project_id=project_id), dry_run)
            facts += f
            moved += m
        verb = "would add" if dry_run else "added"
        self.stdout.write(
            f"{verb} {facts} progress fact(s); "
            f"{'would move' if dry_run else 'moved'} last_progress_at on {moved} task(s)."
        )

    def _project(self, tasks, dry_run: bool) -> tuple[int, int]:
        rows = {
            r["task_id"]: r
            for r in tasks.values("task_id", "parent_task_id", "ts_created_at", "last_progress_at")
        }

        def lookup(task_id):
            row = rows.get(task_id)
            return None if row is None else (row["parent_task_id"],)

        chains = {
            # dict.fromkeys: a cycle must not list a task twice
            task_id: list(dict.fromkeys([task_id, *ancestor_ids(row["parent_task_id"], lookup)]))
            for task_id, row in rows.items()
        }
        # A task with no history starts at its creation.
        latest = {task_id: row["ts_created_at"] for task_id, row in rows.items()}
        pending: list[TaskProgressFact] = []

        def flush() -> int:
            """Insert what is not there yet; return how many that was."""
            if not pending:
                return 0
            have = set(
                TaskProgressFact.objects.filter(
                    activity_id__in={f.activity_id for f in pending}
                ).values_list("activity_id", "ancestor_task_id")
            )
            new = sum(1 for f in pending if (f.activity_id, f.ancestor_task_id) not in have)
            if not dry_run:
                TaskProgressFact.objects.bulk_create(pending, ignore_conflicts=True)
            pending.clear()
            return new

        added = 0
        activities = (
            TaskActivity.objects.filter(task_id__in=rows)
            .order_by("activity_id")
            .values(
                "activity_id", "task_id", "actor_id", "action_type", "metadata", "ts_created_at"
            )
        )
        for a in activities.iterator(chunk_size=_BATCH):
            if not counts_as_progress(a["metadata"]):
                continue
            for depth, ancestor in enumerate(chains[a["task_id"]]):
                pending.append(
                    TaskProgressFact(
                        ancestor_task_id=ancestor,
                        source_task_id=a["task_id"],
                        activity_id=a["activity_id"],
                        actor_id=a["actor_id"],
                        action_type=a["action_type"],
                        depth=depth,
                        ts=a["ts_created_at"],
                    )
                )
                if a["ts_created_at"] > latest[ancestor]:
                    latest[ancestor] = a["ts_created_at"]
            if len(pending) >= _BATCH:
                added += flush()
        added += flush()

        stale = [
            task_id
            for task_id, row in rows.items()
            if row["last_progress_at"] is None or row["last_progress_at"] < latest[task_id]
        ]
        if not dry_run:
            for i in range(0, len(stale), _BATCH):
                objs = list(TaskMaster.objects.filter(task_id__in=stale[i : i + _BATCH]))
                for obj in objs:
                    obj.last_progress_at = latest[obj.task_id]
                # bulk_update does not run save(): ts_updated_at stays put.
                TaskMaster.objects.bulk_update(objs, ["last_progress_at"], batch_size=_BATCH)
        return added, len(stale)

"""`python manage.py purge_orphaned_task_files` - remove task files that no row names (MIP-1824).

A hard delete of a task, of a project or of a demo team used to remove the task's attachment and
body-image rows and leave the files on the media volume. `task_files` stops new ones; this removes
the ones already there. What counts as an orphan, what is never touched and why: see
`origin/services/orphaned_task_files.py`.

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` needs `--yes`. At most `--limit` files
(default 100, never more than 1000) are deleted per run. Safe to run twice. Output is COUNTS ONLY:
no file name, no task id, no text.

Runbook (owner), production, count first:

    railway ssh --service backend-django
    python manage.py purge_orphaned_task_files                    # counts only
    python manage.py purge_orphaned_task_files --apply --yes --limit 100

Files of a task that still exists are NOT deleted unless `--include-live-tasks` is passed (MIP-1890).
"""

from __future__ import annotations

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.services import orphaned_task_files as svc


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or delete (--apply --yes) task files in storage that no row "
        "names and that are older than seven days. Counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the orphaned files. Without this flag the command only counts.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --apply (it deletes files from storage for good).",
        )
        parser.add_argument(
            "--include-live-tasks",
            action="store_true",
            help="Also delete unnamed files of tasks that still exist (kept by default: a live "
            "task's body may still show them). Files whose name is in the task's body or "
            "comments are kept either way.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=svc.DEFAULT_LIMIT,
            help=f"The most files one run deletes (default {svc.DEFAULT_LIMIT}, max {svc.MAX_LIMIT}).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply deletes files from storage for good: add --yes.")
        if not 1 <= options["limit"] <= svc.MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {svc.MAX_LIMIT}.")

        report = svc.scan(
            apply=apply_changes,
            limit=options["limit"],
            include_live_tasks=options["include_live_tasks"],
        )
        say = self.stdout.write

        say(
            "purge_orphaned_task_files: "
            + ("APPLY" if apply_changes else "DRY RUN")
            + f" (no row names it, older than {svc.GRACE.days} days, at most {report.limit} per run)"
        )
        say(f"  {report.scanned} file(s) under the task folders")
        say(
            f"  {report.orphan} orphaned file(s) ({report.orphan_task_gone} of a task that is gone, "
            f"{report.orphan_task_live} of a live task), {_mb(report.orphan_bytes)}"
        )
        if apply_changes:
            say(f"  {report.removed} file(s) deleted ({_mb(report.removed_bytes)})")
            if report.errors:
                say(self.style.WARNING(f"  {report.errors} file(s) could not be deleted (kept)"))
        else:
            say(f"  {report.selected} file(s) would be deleted by --apply --yes")
        if report.waiting:
            say(f"  {report.waiting} more orphan(s) waiting for a later run (limit)")
        if report.kept:
            say("  kept (not deleted), by reason:")
            for reason, count in sorted(report.kept.items(), key=lambda kv: -kv[1]):
                say(f"    {count} {reason}")
        if not apply_changes and report.selected:
            say("  (dry run: pass --apply --yes to delete)")

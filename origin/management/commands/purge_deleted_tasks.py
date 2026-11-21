"""`python manage.py purge_deleted_tasks` - hard-delete tasks that were "Deleted" over 30 days ago (MIP-1785).

The app's delete only sets `status = "Deleted"`; until now nothing ever removed such a task, its
files, its comments or its history. A task stays restorable for 30 days; after that this removes it
and what hangs off it. What counts as "30 days", what is never touched and what goes with a task:
see `origin/services/deleted_task_purge.py`.

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` needs `--yes`. At most `--limit`
tasks (default 20) are purged per run, so a first run is small and a cron drains the rest over the
following runs. Safe to run twice. Output is COUNTS ONLY: no title, id, file name or text.

Runbook (owner), production, count first:

    railway ssh --service backend-django
    python manage.py purge_deleted_tasks                       # counts only
    python manage.py purge_deleted_tasks --apply --yes --limit 5

The collaborative bodies live in genos-collab's `yjs_documents`, which this command does not touch:
after a purge, run genos-collab's `npm run sweep:orphans` (dry run), then with `--delete`.
"""

from __future__ import annotations

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.services import deleted_task_purge as purge

# The order counts are printed in (and the words they are printed with).
REMOVES = (
    ("attachments", "attachment(s)"),
    ("body images", "body image(s)"),
    ("note files", "task note file(s)"),
    ("notes", "task note(s)"),
    ("comments", "comment(s)"),
    ("chat messages", "chat message(s) blanked (task card and comment copies)"),
    ("inbox rows", "inbox row(s)"),
    ("dependencies", "dependency link(s)"),
    ("delivery links", "delivery link(s)"),
    ("history rows", "history row(s)"),
    ("search chunks", "search chunk(s)"),
)
LEFT = (
    ("agent conversations", "agent conversation(s) about the task"),
    ("member counters", "per-member counter row(s) (ids and numbers only)"),
    ("chat replies that are not comments", "chat reply(ies) in the task card's thread"),
)


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or purge (--apply --yes) tasks whose status has been "
        "Deleted for more than 30 days, with their files, comments and history. Counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Purge. Without this flag the command only counts and writes nothing.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --apply (it deletes tasks, rows and uploaded files for good).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=purge.DEFAULT_LIMIT,
            help=f"The most tasks one run purges (default {purge.DEFAULT_LIMIT}).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply deletes tasks and files for good: add --yes.")
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")

        report = purge.purge_deleted_tasks(apply=apply_changes, limit=options["limit"])
        say = self.stdout.write

        say(
            "purge_deleted_tasks: "
            + ("APPLY" if apply_changes else "DRY RUN")
            + f" (tasks Deleted for more than {report.grace_days} days, at most {report.limit} per run)"
        )
        say(f"  {report.scanned} task(s) have status Deleted")
        if apply_changes:
            say(f"  {report.purged} task(s) purged")
        else:
            say(f"  {report.selected} task(s) would be purged by --apply --yes")
        if report.waiting:
            say(f"  {report.waiting} more past every check, waiting for a later run (limit)")
        verb = "removed" if apply_changes else "would be removed"
        for key, label in REMOVES:
            say(f"    {report.counts[key]} {label} {verb}")
        if apply_changes:
            say(f"    {report.files_removed} stored file(s) deleted")
            if report.file_errors:
                say(
                    self.style.WARNING(
                        f"    {report.file_errors} stored file(s) could not be deleted "
                        "(their rows are gone; see the log)"
                    )
                )
        if report.files_kept:
            say(
                f"    {report.files_kept} stored file(s) left: path not under the task's own "
                "folder, or another row names the same file"
            )
        say("  left in place (not removed by this command):")
        for key, label in LEFT:
            say(f"    {report.left[key]} {label}")
        say("    the collaborative bodies (genos-collab `npm run sweep:orphans`)")
        if report.kept:
            say("  kept (not purged), by reason:")
            for reason, count in sorted(report.kept.items(), key=lambda kv: -kv[1]):
                say(f"    {count} {reason}")
        if not apply_changes and report.selected:
            say("  (dry run: pass --apply --yes to purge)")

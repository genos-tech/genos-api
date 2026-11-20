"""`python manage.py purge_detached_note_attachments` - remove uploaded note files whose note is gone (MIP-1720).

A note delete used to leave its attachment rows behind with `note = NULL`, and the uploaded files
with them (`origin/services/detached_note_attachments.py` says why such a row is unreachable and
what is checked before one is removed). MIP-1720 stops new ones on every note delete path; this
removes the old ones, for personal, task and chat notes.

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` needs `--yes`. Safe to run twice.
Output is counts only: no file name, note id or title is printed.

Runbook (owner), production, count first:

    railway ssh --service <api service>
    python manage.py purge_detached_note_attachments              # counts only
    python manage.py purge_detached_note_attachments --apply --yes
"""

from django.core.management.base import BaseCommand, CommandError

from origin.services.detached_note_attachments import purge_detached_note_attachments


class Command(BaseCommand):
    help = "Delete attachment rows and files of notes that no longer exist (note IS NULL)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the rows and files. Without this flag the command only counts.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --apply (it deletes rows and uploaded files for good).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply deletes rows and files for good: add --yes.")

        report = purge_detached_note_attachments(apply=apply_changes)

        verb = "deleted" if apply_changes else "would be deleted"
        for label, count in report.removable.items():
            self.stdout.write(f"  {count} {label} note attachment(s) {verb}")
        self.stdout.write(
            f"  kept: {report.kept_odd_path} with a path that is not notes/<family>/<id>/<file>, "
            f"{report.kept_note_alive} whose note still exists, "
            f"{report.kept_file_shared} whose file another row uses"
        )
        if apply_changes:
            self.stdout.write(f"  {report.files_removed} file(s) removed")
            if report.file_errors:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {report.file_errors} row(s) KEPT: their file could not be deleted; "
                        "run again"
                    )
                )
        elif report.total:
            self.stdout.write("re-run with --apply --yes to delete them")

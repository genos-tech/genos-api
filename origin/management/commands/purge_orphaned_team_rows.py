"""`python manage.py purge_orphaned_team_rows` — remove what earlier demo teardowns left behind (MIP-1704).

Until MIP-1702 (api #949) every demo teardown left the mention groups, note folder tags, invites
(an invited e-mail address) and custom emojis (an image file) a visitor had made, with
`team = NULL`. The fix stops new ones; this removes the old ones. Why only those four tables, and
how an orphaned custom emoji is told from a global default emoji (which also has `team = NULL`):
see `origin/services/orphaned_team_rows.py`.

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` needs `--yes`. Safe to run twice (the
second run reports 0). The report also lists every OTHER table that has `team IS NULL` rows; those
are only counted, never deleted: read them before deciding anything about them.

Runbook (owner), production, count first:

    railway ssh --service <api service>
    python manage.py purge_orphaned_team_rows              # counts only
    python manage.py purge_orphaned_team_rows --apply --yes

After #949 is deployed the counts must stop growing: run the dry run again some days later and
expect 0.
"""

from django.core.management.base import BaseCommand, CommandError

from origin.services.orphaned_team_rows import purge_orphaned_team_rows


class Command(BaseCommand):
    help = "Delete mention groups, note folder tags, invites and custom emojis whose team is gone."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the rows. Without this flag the command only counts.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --apply (it deletes rows and image files for good).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply deletes rows and image files for good: add --yes.")

        report = purge_orphaned_team_rows(apply=apply_changes)

        verb = "deleted" if apply_changes else "would be deleted"
        self.stdout.write(f"  {report.mention_groups} mention group(s) {verb}")
        self.stdout.write(f"  {report.note_folder_tags} note folder tag(s) {verb}")
        self.stdout.write(f"  {report.invites} invite(s) {verb}")
        self.stdout.write(f"  {report.custom_emojis} custom emoji(s) {verb}")
        if apply_changes:
            self.stdout.write(f"  {report.emoji_files_removed} emoji image file(s) removed")
            if report.emoji_file_errors:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {report.emoji_file_errors} emoji(s) KEPT: their image could not be "
                        "deleted (see the log); run again"
                    )
                )
        if report.survey:
            self.stdout.write("  other tables with team = NULL rows (counted only, NOT deleted):")
            for label, count in report.survey.items():
                self.stdout.write(f"    {label}: {count}")
        else:
            self.stdout.write("  no other table has team = NULL rows")
        if not apply_changes:
            self.stdout.write("  (dry run: pass --apply --yes to delete)")

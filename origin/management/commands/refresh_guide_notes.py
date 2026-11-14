"""Give existing teams the new text of the two MCP guide notes (MIP-1558).

Guide notes are seeded once per member and team, and an existing copy is the
member's own note, so the guide never rewrites one. The two notes that teach MCP
("Connecting Genos to other tools" and "API keys and webhooks", English and
Japanese) now lead with connecting an AI app by signing in (no API key). This
command brings the copies that already exist up to date, with one rule:

    a copy is replaced ONLY if its text is still byte for byte a text we shipped
    (block ids aside). A copy anyone has edited is left exactly as it is.

A replaced note keeps what it had as a version (restorable from the note's
history), is saved and versioned like any agent edit, and is pushed into the
editor's collaborative document after the commit. Seeding of new copies is
unchanged and needs nothing from this command.

Needs `MCP_OAUTH_ENABLED` and `API_PUBLIC_BASE_URL` (the text carries the MCP
address); without them it refuses, because the new text would have no OAuth section.

Dry-run by default; pass --apply to write. Idempotent: a second run finds the
notes already current.
"""

from django.core.management.base import BaseCommand, CommandError

from origin.services.guide_notes import refresh_unedited_connect_notes


class Command(BaseCommand):
    help = "Replace unedited copies of the two MCP guide notes with the new text."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only counts.",
        )
        parser.add_argument(
            "--team",
            default=None,
            help="Optional team_id to scope the run.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        try:
            report = refresh_unedited_connect_notes(apply=apply_changes, team_id=options["team"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        verb = "replaced" if apply_changes else "would be replaced"
        self.stdout.write(f"  {len(report.replaced)} unedited note(s) {verb}")
        self.stdout.write(f"  {len(report.already_current)} already have the new text")
        self.stdout.write(
            f"  {len(report.left_alone)} edited by their owner (or an older text): left alone"
        )
        if report.changed_underneath:
            self.stdout.write(
                f"  {len(report.changed_underneath)} changed while this ran: left alone "
                f"(note ids {report.changed_underneath})"
            )
        if apply_changes:
            pushes = ", ".join(f"{k}={v}" for k, v in sorted(report.pushes.items())) or "none"
            self.stdout.write(f"  editor push: {pushes}")
        mode = "APPLIED" if apply_changes else "DRY RUN (pass --apply to write)"
        self.stdout.write(f"{mode}: {len(report.replaced)} note(s)")

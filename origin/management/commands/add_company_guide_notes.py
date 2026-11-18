"""Give existing members the two company guide notes (MIP-1689).

The Genos Guide is seeded once per member and team, so the two notes added later
("Organising a large workspace" and "Setting up Genos for your company", English and
Japanese) never reach anyone who already has the folder. This command makes each of them in
every existing guide folder that does not have it yet, and touches nothing else.

Dry-run by default; pass --apply to write. Run it ONCE, after the deploy: it is not
idempotent against deletion (a member who deleted a company note gets it again).
"""

from django.core.management.base import BaseCommand

from origin.services.guide_notes import add_company_guide_notes


class Command(BaseCommand):
    help = "Add the two company guide notes to existing guide folders that lack them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the notes. Without this flag the command only counts.",
        )
        parser.add_argument(
            "--team",
            default=None,
            help="Optional team_id to scope the run.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        report = add_company_guide_notes(apply=apply_changes, team_id=options["team"])
        verb = "added" if apply_changes else "would be added"
        self.stdout.write(f"  {len(report.added)} note(s) {verb}")
        self.stdout.write(f"  {report.already_there} already there")
        if not apply_changes:
            self.stdout.write("  (dry run: pass --apply to write)")

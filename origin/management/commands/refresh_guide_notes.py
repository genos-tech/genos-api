"""Bring existing Genos Guide folders up to date with today's guide (MIP-2325).

Guide notes are seeded once per member and team, and a copy is the member's own note, so a fix to the
guide never reached anyone who already had the folder. For every existing guide folder (English and
Japanese) this command:

    replaces a copy whose text is still exactly a text we shipped (the old text is kept as a version),
    adds a note the folder was never given (one the member deleted is NOT brought back),
    leaves a copy the member edited alone, and counts it.

Dry-run by default and prints COUNTS ONLY (no titles, owners or note ids): pass --apply to write.
Idempotent: a second run finds everything current. --team scopes a run to one team_id.
See `origin/services/guide_refresh.py`. (It replaces MIP-1558's two-note refresh.)
"""

from django.core.management.base import BaseCommand

from origin.services.guide_refresh import refresh_guide_notes


class Command(BaseCommand):
    help = (
        "Replace unedited guide copies with today's text and add missing guide notes "
        "(dry run by default)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only counts.",
        )
        parser.add_argument("--team", default=None, help="Optional team_id to scope the run.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        report = refresh_guide_notes(apply=apply_changes, team_id=options["team"])
        w = self.stdout.write
        w(f"  {report.folders} guide folder(s) scanned")
        w(f"  {report.replaced} unedited note(s) {'replaced' if apply_changes else 'to replace'}")
        w(f"  {report.added} missing note(s) {'added' if apply_changes else 'to add'}")
        w(f"  {report.already_current} already have today's text")
        w(f"  {report.edited_left} edited by their owner: left alone")
        w(f"  {report.deleted_left} deleted by their owner: not brought back")
        if report.changed_underneath:
            w(f"  {report.changed_underneath} changed while this ran: left alone")
        if apply_changes:
            pushes = ", ".join(f"{k}={v}" for k, v in sorted(report.pushes.items())) or "none"
            w(f"  editor push: {pushes}")
        w("APPLIED" if apply_changes else "DRY RUN (pass --apply to write)")

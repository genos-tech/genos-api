"""Keep the Genos Hub's counters and search columns true (MIP-2047). Run it nightly.

    python manage.py skills_refresh_hub              # recompute "uses", drop run rows older than 90 days
    python manage.py skills_refresh_hub --rebuild    # also rewrite the directory columns of every public skill

"Uses" is the number of DISTINCT accounts that ran a public skill in the last 30 days. A run adds to
it as it happens (`hub.record_run`); this makes it exact and lets it fall as runs age out. `--rebuild`
writes the search and tag columns of every public skill from its published version: the way to fill
them for skills that were public before the Hub existed. Safe to run twice; it never changes a
skill's text, visibility or status.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.services.skills import hub


class Command(BaseCommand):
    help = "Recompute the Hub's uses counter and drop old run rows; --rebuild rewrites the listing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Also rewrite the directory columns of every public skill.",
        )

    def handle(self, *args, **options):
        counts = hub.refresh_run_counts()
        self.stdout.write(
            f"uses: {counts['updated']} updated, {counts['zeroed']} back to zero, "
            f"{counts['dropped']} old run rows dropped"
        )
        if options["rebuild"]:
            self.stdout.write(f"listing: {hub.rebuild_all_listings()} public skills rewritten")

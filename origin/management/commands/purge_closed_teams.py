"""`python manage.py purge_closed_teams` - delete ALL data of teams that have no live member (MIP-1958).

Ken's decision: a team with no member left has everything permanently deleted. The request that
removes the last member only closes the team; this command (the daily cron `closed-team-purge`,
and the one-off for teams closed before this change) deletes the data. What goes and what is kept:
see `origin/services/team_purge.py`.

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` needs `--yes`. At most `--limit`
teams are purged per run (default 20), so a first run is small and the cron drains the rest. Safe
to run twice. Output is COUNTS ONLY: no team name, id, title, file name or address.

Runbook (owner), production, count first:

    railway ssh --service backend-django
    python manage.py purge_closed_teams                         # counts only
    python manage.py purge_closed_teams --apply --yes --limit 1 # then raise the limit
"""

from __future__ import annotations

import logging
from collections import Counter

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.services import team_purge

log = logging.getLogger(__name__)
DEFAULT_LIMIT = 20


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or purge (--apply --yes) closed teams with no live member, "
        "with all their data, files, search entries and collab documents. Counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Purge. Without this flag the command only counts."
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm --apply (it deletes teams, rows and stored files for good).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"The most teams one run purges (default {DEFAULT_LIMIT}).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply deletes teams and files for good: add --yes.")
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")

        say = self.stdout.write
        pending = team_purge.pending_purge_team_ids()
        batch = pending[: options["limit"]]
        say(
            "purge_closed_teams: "
            + ("APPLY" if apply_changes else "DRY RUN")
            + f" (at most {options['limit']} per run)"
        )
        say(f"  {len(pending)} closed team(s) with no live member")

        totals: Counter = Counter()
        purged = 0
        failed = 0
        for team_id in batch:
            if apply_changes:
                try:
                    outcome = team_purge.purge_team(team_id)
                except Exception as exc:  # noqa: BLE001 - one bad team must not stop the others
                    failed += 1
                    log.error("purge_closed_teams: a team failed (%s)", type(exc).__name__)
                    continue
                if outcome.purged:
                    purged += 1
                    totals.update(outcome.counts)
                    totals["stored files"] += outcome.files
                    totals["collab documents"] += outcome.collab_documents
            else:
                totals.update(team_purge.survey(team_id))
        if apply_changes:
            say(f"  {purged} team(s) purged")
        else:
            say(f"  {len(batch)} team(s) would be purged by --apply --yes")
        if len(pending) > len(batch):
            say(f"  {len(pending) - len(batch)} more waiting for a later run (limit)")
        verb = "removed" if apply_changes else "would be removed"
        for key, count in totals.items():
            say(f"    {count} {key} {verb}")
        kept_for_billing = team_purge.held_team_count()
        if kept_for_billing:
            say(f"  {kept_for_billing} team(s) held, not purged: billing (a paid Stripe plan)")
        if failed:
            say(self.style.ERROR(f"  {failed} team(s) failed (see the log); they stay pending"))

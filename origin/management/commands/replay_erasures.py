"""`python manage.py replay_erasures` - re-apply erasures after a backup restore (MIP-2249).

A restore brings back accounts, teams, tasks and messages that were erased after the backup was
taken. This command reads the ids-only erasure log and runs the same deletion services again
(`delete_account`, `purge_team`, the deleted-task purge, `tombstone_message`).

DRY RUN IS THE DEFAULT: it counts, writes nothing. `--apply` erases. Idempotent: run it twice and
the second run finds everything gone. Output is COUNTS ONLY.

The log sits in the database a restore replaces, so export it from the LIVE database before the
swap and replay from that file after:

    python manage.py replay_erasures --export erasures.json            # before the swap
    python manage.py replay_erasures --from-file erasures.json --since 2026-11-20T03:00:00Z
    python manage.py replay_erasures --from-file erasures.json --since ... --apply

`--since` is the backup's time: only erasures at or after it are replayed. The file holds object
types, ids and times only, no personal data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from origin.models.common.erasure_models import ErasureEvent
from origin.services import erasure_log


def _since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        raise CommandError("--since must be an ISO time, e.g. 2026-11-20T03:00:00Z")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class Command(BaseCommand):
    help = "Dry run (default) or --apply: re-run the logged erasures after a backup restore."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since", help="The backup's time (ISO). Replay erasures from then on."
        )
        parser.add_argument("--from-file", help="Read the log from a file made by --export.")
        parser.add_argument("--export", help="Write the log (ids and times only) to this file.")
        parser.add_argument("--apply", action="store_true", help="Erase. Without it: counts only.")

    def handle(self, *args, **options):
        since = _since(options["since"])
        say = self.stdout.write

        if options["export"]:
            events = erasure_log.export_events(since)
            with open(options["export"], "w", encoding="utf-8") as fh:
                json.dump(events, fh)
            say(f"replay_erasures: exported {len(events)} erasure event(s) (ids and times only)")
            return

        if options["from_file"]:
            events = erasure_log.load_events(options["from_file"], since)
        else:
            if since is None:
                raise CommandError("--since <backup time> is required (or use --from-file).")
            events = erasure_log.export_events(since)

        apply_changes = options["apply"]
        say("replay_erasures: " + ("APPLY" if apply_changes else "DRY RUN"))
        say(f"  {len(events)} logged erasure(s) in the window")
        result = erasure_log.replay(events, apply=apply_changes)
        for key, count in sorted(result.items()):
            say(f"    {count} {key}")
        if not apply_changes and result:
            say("  (dry run: pass --apply to erase)")
        say(f"  log rows in this database: {ErasureEvent.objects.count()}")

"""`python manage.py purge_deleted_team_chunks` -- remove the search chunks of teams that no longer exist (MIP-2384).

A team that is deleted for good must leave no searchable or stored text. The team purge and the
signal on `TeamMaster` remove a team's chunks when the row goes; this removes the ones that were
left behind before (production held 122 chunks of 12 deleted teams, 116 `conversation` and 6
`spotlight_answer`). The orphan sweep that runs after every reindex pass now reads such a chunk as
dead too, so this is the explicit, countable version for one reviewed run.

    python manage.py purge_deleted_team_chunks              # dry run (default): counts only
    python manage.py purge_deleted_team_chunks --apply      # delete them

  * A dead team is a `team_id` tracked in `RagChunk` with no `TeamMaster` row. A soft-closed team
    still has its row and is NOT touched (that is `purge_closed_teams`).
  * A dry run changes nothing and prints COUNTS ONLY: dead teams, chunks by entity type, and how
    many `AgentRun` rows still carry a dead team id (reported, never deleted here: they are a
    separate decision).
  * `--apply` deletes the documents in OpenSearch first and the tracking rows of the ones that
    were deleted (a failed delete keeps its row, so run it again), then counts again and exits
    non-zero if a tracking row survived.

Idempotent. Production runbook (owner), count first:

    railway ssh --service backend-django
    python manage.py purge_deleted_team_chunks
    python manage.py purge_deleted_team_chunks --apply
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine.models import AgentRun, RagChunk
from origin.search_engine.purge import dead_team_ids, purge_chunks

_BATCH = 500


def _chunk_ids(team_ids: list) -> list[str]:
    ids: list[str] = []
    for i in range(0, len(team_ids), _BATCH):
        ids.extend(
            RagChunk.objects.filter(team_id__in=team_ids[i : i + _BATCH]).values_list(
                "chunk_id", flat=True
            )
        )
    return ids


def _by_type(team_ids: list) -> Counter:
    counts: Counter = Counter()
    for i in range(0, len(team_ids), _BATCH):
        for etype in RagChunk.objects.filter(team_id__in=team_ids[i : i + _BATCH]).values_list(
            "entity_type", flat=True
        ):
            counts[etype] += 1
    return counts


def _agent_runs(team_ids: list) -> int:
    strs = [str(t) for t in team_ids]
    return sum(
        AgentRun.objects.filter(team_id__in=strs[i : i + _BATCH]).count()
        for i in range(0, len(strs), _BATCH)
    )


class Command(BaseCommand):
    help = (
        "Delete the search chunks (OpenSearch + RagChunk) of teams that no longer exist. "
        "Dry run by default; --apply deletes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Delete the chunks. Without it: counts only."
        )
        parser.add_argument("--dry-run", action="store_true", help="Count only (the default).")

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("--apply and --dry-run exclude each other.")
        apply_changes = options["apply"]

        teams = dead_team_ids()
        chunk_ids = _chunk_ids(teams)
        verb = "deleted" if apply_changes else "would be deleted"
        self.stdout.write(f"{len(teams)} deleted team(s) still have chunks")
        for etype, n in sorted(_by_type(teams).items()):
            self.stdout.write(f"  {n} {etype} chunk(s) {verb}")
        self.stdout.write(f"  {len(chunk_ids)} chunk(s) in total {verb}")
        self.stdout.write(
            f"  {_agent_runs(teams)} AgentRun row(s) carry a deleted team id "
            "(reported only, not touched here)"
        )
        if not apply_changes:
            self.stdout.write("Dry run: nothing changed. Add --apply to delete.")
            return

        removed = purge_chunks(chunk_ids) if chunk_ids else 0
        left = len(_chunk_ids(teams))
        self.stdout.write(f"{removed} tracking row(s) removed; {left} left")
        if left:
            raise CommandError(
                f"{left} chunk(s) could not be deleted from OpenSearch; run the command again."
            )

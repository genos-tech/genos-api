"""`python manage.py purge_scoped_team_answers` -- take scoped asks out of the team lane.

An answer to "Ask about this thread / note" must never sit in the team-shared
`spotlight_answer` lane (MIP-978; the rule is `shared_answers.is_scoped_session`).
New asks are no longer collected, and the orphan sweep that runs after every
reindex pass removes the copies it can see (`RagChunk` rows), within one cron
tick of the deploy. This command is the explicit, countable version:

    python manage.py purge_scoped_team_answers              # dry run (default)
    python manage.py purge_scoped_team_answers --dry-run
    python manage.py purge_scoped_team_answers --apply

  * It finds the runs of every scoped session straight from the database, with
    the very filter the sweep uses (`shared_answers.scoped_session_q`), so it
    does not depend on the tracking table alone.
  * A dry run changes nothing and prints COUNTS ONLY (never an id, a question
    or an answer): the scoped runs, and how many of them still have a tracked
    team-lane chunk, which is the number of answers that were collected.
  * `--apply` deletes the `spotlight_answer:<run_id>` chunk of each of those
    runs by its deterministic id, so a document with no tracking row goes too
    (a delete of an id that was never indexed is a 404, counted as success). The
    private `conversation` lane is untouched. It then counts again and fails if
    a tracking row survived, which only happens when its delete failed: run it
    again.

Idempotent and safe to re-run. It never reads or writes the run rows.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine import shared_answers
from origin.search_engine.models import AgentRun, RagChunk
from origin.search_engine.opensearch_client import get_client
from origin.search_engine.purge import purge_agent_runs

LANE = "spotlight_answer"
_BATCH = 500


def scoped_run_ids() -> list[str]:
    """Every run (whatever its status) of a scoped session: the superset that
    can have a team-lane chunk."""
    return [
        str(run_id)
        for run_id in AgentRun.objects.filter(shared_answers.scoped_session_q())
        .order_by("started_at")
        .values_list("run_id", flat=True)
    ]


def tracked_chunk_count(run_ids: list[str]) -> int:
    """How many of these runs still have a tracked team-lane chunk."""
    total = 0
    for i in range(0, len(run_ids), _BATCH):
        entity_ids = [f"{LANE}:{rid}" for rid in run_ids[i : i + _BATCH]]
        total += RagChunk.objects.filter(entity_type=LANE, entity_id__in=entity_ids).count()
    return total


class Command(BaseCommand):
    help = (
        "Remove the team-lane (spotlight_answer) chunks of runs asked from a thread or a note. "
        "Dry run unless --apply; prints counts only."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Count only; change nothing (the default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Delete the chunks.",
        )

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        run_ids = scoped_run_ids()
        report = {
            "mode": "apply" if apply else "dry-run",
            "scoped_runs": len(run_ids),
            "tracked_team_chunks": tracked_chunk_count(run_ids),
        }

        if apply and run_ids:
            # `purge_agent_runs` is best-effort and never raises, so an
            # unreachable index would look like success. Say so up front.
            if not get_client().ping():
                raise CommandError("OpenSearch is unreachable (ping failed); nothing purged.")
            for i in range(0, len(run_ids), _BATCH):
                purge_agent_runs(run_ids[i : i + _BATCH], lanes=(LANE,))
            report["tracked_team_chunks_remaining"] = tracked_chunk_count(run_ids)

        self.stdout.write(json.dumps(report, indent=2))
        if report.get("tracked_team_chunks_remaining"):
            raise CommandError(
                f"{report['tracked_team_chunks_remaining']} tracked chunk(s) could not be "
                "deleted; run the command again."
            )
        if not apply:
            self.stdout.write(self.style.WARNING("Dry run: nothing changed. Add --apply to purge."))

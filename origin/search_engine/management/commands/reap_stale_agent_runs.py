"""`python manage.py reap_stale_agent_runs` — close abandoned runs.

An `AgentRun` is created `status="running"` and closed by the streaming
view at end-of-stream. Two things can leave a row stuck in `running`
with a NULL `finished_at` forever:

  * the worker process died mid-run (deploy, OOM, Cloud Run scale-in) —
    nothing is left alive to write the terminal state;
  * a pre-cancellation disconnect, for rows created before the
    `GeneratorExit` handler in `_stream_ndjson` existed.

Stuck rows are not cosmetic. Every "cost per completed run" or "runs by
outcome" figure divides by a denominator that these silently inflate on
one side and never on the other, and they accumulate forever because
nothing else looks at them: the chunkers and the judge sampler both
filter `status="done"` exactly, so an abandoned row is invisible right
up until someone tries to do arithmetic with it.

This marks them `status="error"` with an explicit message, which is
honest — we do not know what happened, only that nobody closed it.
Deliberately NOT `cancelled`: that status means "the client
disconnected and we observed it", which is a thing we can prove only
from the live handler.

    python manage.py reap_stale_agent_runs                 # older than 2h
    python manage.py reap_stale_agent_runs --hours 6
    python manage.py reap_stale_agent_runs --dry-run

Scheduled hourly (genos-platform `jobs.tf`). Safe to re-run: it only
ever touches rows still in `running`, so a second pass finds nothing.

The threshold must comfortably exceed the longest legitimate run. A run
is bounded by AGENT_MAX_STEPS model calls plus tools; two hours is
orders of magnitude above that, because the cost of reaping a LIVE run
(a user watching their answer get marked failed underneath them) is far
worse than the cost of a stale row living an extra hour.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.search_engine.models import AgentRun

log = logging.getLogger(__name__)

_STALE_MESSAGE = (
    "Run never reached a terminal state — closed by reap_stale_agent_runs. "
    "The worker most likely died mid-run (deploy, OOM, or scale-in)."
)


class Command(CronCommand):
    help = "Close AgentRun rows stuck in 'running' past a cutoff."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=2,
            help="Age in hours past which a still-running run is considered stale (default 2).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be closed without writing.",
        )

    def handle(self, *args, **options):
        hours = max(1, int(options["hours"]))
        dry_run = bool(options["dry_run"])
        cutoff = timezone.now() - timedelta(hours=hours)

        stale = AgentRun.objects.filter(status="running", started_at__lt=cutoff)
        rows = list(stale.values_list("run_id", "team_id", "started_at")[:200])
        if not rows:
            self.stdout.write(f"No runs stuck in 'running' older than {hours}h.")
            return

        self.stdout.write(f"Found {len(rows)} stale run(s) older than {hours}h:")
        for run_id, team_id, started in rows:
            self.stdout.write(f"  {run_id}  team={team_id or '(none)'}  started={started:%Y-%m-%d %H:%M}")

        if dry_run:
            self.stdout.write(self.style.NOTICE("--dry-run: nothing written."))
            return

        # A single UPDATE, re-filtered on status so a run that closed
        # itself between the SELECT above and now is left alone.
        closed = stale.update(
            status="error",
            error_message=_STALE_MESSAGE,
            finished_at=timezone.now(),
        )
        # WARNING, not ERROR: stale rows are an expected consequence of
        # deploys and scale-in, and reaping them is this job succeeding.
        # An ERROR here would red the cron every time it did its job
        # (see CronCommand's tripwire).
        log.warning("reap_stale_agent_runs closed %s stale run(s) older than %sh", closed, hours)
        self.stdout.write(self.style.SUCCESS(f"Closed {closed} stale run(s)."))

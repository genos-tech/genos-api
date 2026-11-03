"""When is a `running` `AgentRun` abandoned? One rule, two readers.

An `AgentRun` is created `status="running"` and closed by the streaming view
at end-of-stream. If the worker dies first (gunicorn recycle, deploy, OOM) the
row stays `running` with a NULL `finished_at` forever. Two things need to
tell "still streaming" from "abandoned":

  * `reap_stale_agent_runs` closes the abandoned ones (hourly cron);
  * deleting a person's conversations (`_delete_agent_conversations`) refuses
    to run under a live worker, and must NOT be blocked by a dead one. A
    person's right to erase their conversations cannot depend on a cron.

Both go through this module, so the two cannot drift: one threshold, one
timestamp, one comparison.

The rule: a run is stale when `status == "running"` and `started_at` is
STRICTLY older than the cutoff (`now - STALE_RUN_HOURS`). `started_at` is the
only timestamp that is ever set on a running row: there is no heartbeat or
updated column, and `finished_at` is NULL until the run closes. A run exactly
at the cutoff is not stale yet.

The threshold must comfortably exceed the longest legitimate run. A run is
bounded by AGENT_MAX_STEPS model calls plus tools; two hours is orders of
magnitude above that, because reaping (or deleting under) a LIVE run is far
worse than a stale row living an extra hour. Known edge: a run resumed after
write-approval (POST /decide/) goes back to `running` WITHOUT a new
`started_at`, so one that waited more than this long for the approval looks
stale the moment it resumes.

Read `STALE_RUN_HOURS` through this module at call time (`stale_runs.X`), not
with `from ... import STALE_RUN_HOURS`, so a test (or an operator patching it
in a shell) moves every reader together.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Q
from django.utils import timezone

# Age, in whole hours, past which a still-`running` run is considered
# abandoned. The `--hours` default of `reap_stale_agent_runs` and the
# in-flight check of the conversation delete both read this.
STALE_RUN_HOURS = 2


def stale_cutoff(hours: int | None = None, *, now: datetime | None = None) -> datetime:
    """The instant before which a `running` run started is stale.

    `hours` defaults to `STALE_RUN_HOURS` (read now, not at import) and is
    clamped to at least 1, as the reaper's `--hours` always was.
    """
    hours = max(1, int(STALE_RUN_HOURS if hours is None else hours))
    return (now or timezone.now()) - timedelta(hours=hours)


def _started_before(cutoff: datetime) -> Q:
    return Q(started_at__lt=cutoff)


def stale_running_q(cutoff: datetime) -> Q:
    """Rows still `running` that started before `cutoff`: abandoned."""
    return Q(status="running") & _started_before(cutoff)


def in_flight_q(cutoff: datetime) -> Q:
    """Rows still `running` that are NOT stale: a worker may be writing to
    them. Exactly the `running` rows `stale_running_q` leaves out."""
    return Q(status="running") & ~_started_before(cutoff)

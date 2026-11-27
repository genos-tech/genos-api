"""Start a "Run now" that must not run inside the web request (MIP-2457).

A skill run or a dashboard page can take minutes. Inside the request gunicorn kills the worker at
its timeout (with every other request on it), and a thread in the worker dies when the worker is
recycled. So the run goes to a child process, `manage.py schedule_run_now`, which inherits the
web service's environment (the switches, search and model credentials a skill needs) and outlives
a worker restart. The pattern of `search_engine.team_reindex`.

The caller has already CLAIMED the schedule; the child runs it under that claim and refreshes its
heartbeat. If the child is lost (a deploy replaces the container), the heartbeat stops and the
claim is taken over within about a minute (`schedules.HEARTBEAT_STALE`). Nothing is retried.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings

log = logging.getLogger("origin.schedules")

# The child stops itself at the time limit; the reaper kills one that did not, this much later.
_KILL_GRACE_S = 60

BUSY_SENTENCE = "Genos is busy making other reports. Try again in a minute."
FAILED_TO_START_SENTENCE = "The report could not be started. Try again in a minute."


class RunnerUnavailable(Exception):
    """No child was started. The message is a sentence for the member."""


def _spawn_process(schedule_id: str, claim: str) -> subprocess.Popen:
    """The one place a child is created, so a test can replace it."""
    return subprocess.Popen(  # noqa: S603 - fixed argv; the id is a UUID, the claim a datetime
        [
            sys.executable,
            str(settings.BASE_DIR / "manage.py"),
            "schedule_run_now",
            "--schedule",
            schedule_id,
            "--claim",
            claim,
        ],
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def start(schedule_id: Any, claim: datetime) -> None:
    """Start the child for one claimed schedule and return at once. How many may run at once is
    the caller's to decide (`schedules.run_now` counts them in the database)."""
    sid = str(uuid.UUID(str(schedule_id)))
    try:
        proc = _spawn_process(sid, claim.isoformat())
    except Exception as exc:  # noqa: BLE001 - e.g. no memory for another process
        log.warning("schedule_run_now_not_started schedule=%s error=%s", sid, type(exc).__name__)
        raise RunnerUnavailable(FAILED_TO_START_SENTENCE) from None
    threading.Thread(target=_reap, args=(sid, proc), daemon=True, name="schedule-reaper").start()


def _reap(schedule_id: str, proc: subprocess.Popen) -> None:
    """Wait for the child so it leaves no zombie."""
    limit = int(getattr(settings, "SCHEDULE_RUN_NOW_TIMEOUT_S", 900)) + _KILL_GRACE_S
    try:
        code = proc.wait(timeout=limit)
        if code:
            log.warning("schedule_run_now_exit code=%s schedule=%s", code, schedule_id)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log.warning("schedule_run_now_killed schedule=%s after=%ss", schedule_id, limit)

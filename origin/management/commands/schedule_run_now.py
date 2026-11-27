"""`python manage.py schedule_run_now --schedule <uuid> --claim <iso>` (MIP-2457).

The child process "Run now" starts for a slow report (`services.schedule_runner`). It runs the
schedule the web request claimed, under that claim, and stops itself after
`SCHEDULE_RUN_NOW_TIMEOUT_S`. Not for use by hand: without the matching claim it does nothing.

It always ends normally (exit code 0): the outcome is on the schedule's row, and a web worker's
orphan must never end with an exit code a process manager reads as its own boot failure.
"""

from __future__ import annotations

import logging
import signal
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from apis.request_id import describe_exception
from origin.services import schedules

log = logging.getLogger("origin.schedules")


def _timed_out(signum, frame):
    raise schedules.RunTimedOut


class Command(BaseCommand):
    help = 'Run one claimed schedule (started by "Run now").'

    def add_arguments(self, parser):
        parser.add_argument("--schedule", required=True)
        parser.add_argument("--claim", required=True)

    def handle(self, *args, **options):
        previous = signal.signal(signal.SIGALRM, _timed_out)
        signal.alarm(int(getattr(settings, "SCHEDULE_RUN_NOW_TIMEOUT_S", 900)))
        try:
            claim = datetime.fromisoformat(options["claim"])
            if not schedules.run_claimed_by_id(options["schedule"], claim):
                log.info("schedule_run_now_claim_gone schedule=%s", options["schedule"])
        except schedules.RunTimedOut:
            log.warning("schedule_run_now_timed_out schedule=%s", options["schedule"])
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            log.error(
                "schedule_run_now_crashed schedule=%s error=%s",
                options["schedule"],
                describe_exception(exc),
            )
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

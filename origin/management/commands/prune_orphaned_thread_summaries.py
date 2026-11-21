"""`python manage.py prune_orphaned_thread_summaries` - remove thread summaries that outlived their thread (MIP-1802).

A `ThreadSummary` is the cached LLM digest of a thread's replies. It has no foreign key to the chat
or the message, so until MIP-1802 no delete removed it: a deleted reply, thread or chat left the
digest text behind. New deletes forget it (`services/thread_summary_cleanup`); this finds and removes
the rows the older ones left, and their search chunks.

    python manage.py prune_orphaned_thread_summaries            # DRY RUN: counts only, writes nothing
    python manage.py prune_orphaned_thread_summaries --apply    # delete them

Output is COUNTS ONLY, per reason (see `services/thread_summary_orphans`): no text, id, title or name
is printed. A removed summary is a cache: the next "ask about this thread" writes a new one. Safe to
re-run; one-off, not a cron.
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import thread_summary_orphans as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) thread summaries whose chat, thread or "
        "replies are gone or deleted. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the summaries and their search chunks. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_orphaned_thread_summaries: " + ("APPLY" if apply else "DRY RUN"))
        result = service.prune(apply=apply)
        reasons = ", ".join(f"{name}={n}" for name, n in result.by_reason.items())
        reasons += f", edited_since_digest={result.edited_since_digest} (kept, count only)"
        if apply:
            self.stdout.write(f"removed {result.changed} thread summaries; by reason: {reasons}")
            log.info("prune_orphaned_thread_summaries: removed=%d", result.changed)
        else:
            self.stdout.write(
                f"would remove {result.matched} thread summaries; by reason: {reasons}"
            )
            if result.matched:
                self.stdout.write("re-run with --apply to remove them")

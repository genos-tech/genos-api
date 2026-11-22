"""`python manage.py prune_agent_calls` - expire the agent call log (MIP-2006).

One `AgentCall` row per MCP tool call (ids and tool names only) is kept
`origin.services.agent_calls.RETENTION_DAYS` (90) days. The rule lives in that module.

    python manage.py prune_agent_calls            # DRY RUN: counts only, writes nothing
    python manage.py prune_agent_calls --apply    # delete the expired rows

Output is COUNTS ONLY. Safe to re-run, and safe while the log is switched off (it finds nothing).
A daily cron runs it as `python /app/manage.py prune_agent_calls --apply --heartbeat agent-call-prune`
(the dry run is the default, so a cron that leaves `--apply` out deletes nothing).
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import agent_calls

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or delete (--apply) agent call log rows older than the "
        "retention window. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the expired rows. Without it nothing is written.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        days = agent_calls.RETENTION_DAYS
        self.stdout.write("prune_agent_calls: " + ("APPLY" if apply else "DRY RUN"))
        result = agent_calls.prune(apply=apply)
        if not apply:
            self.stdout.write(f"would delete {result.matched} agent calls older than {days} days")
            if result.matched:
                self.stdout.write("re-run with --apply to delete them")
            return
        log.info("prune_agent_calls: deleted=%d days=%d", result.deleted, days)
        self.stdout.write(f"deleted {result.deleted} agent calls older than {days} days")

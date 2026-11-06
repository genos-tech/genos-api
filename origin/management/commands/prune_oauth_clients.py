"""`python manage.py prune_oauth_clients` — remove OAuth client rows nobody is using (MIP-1104).

Registering a client is open (anyone may, 20 an hour from an address), and a metadata-document
sign-in makes the server store a row the first time it sees an address. Nothing else ever
removes those rows, so without this the table only grows. A row goes when it is older than
`MCP_OAUTH_CLIENT_PRUNE_DAYS` and nothing refers to it: no connection, no token, no pending
code (and, for a metadata document, its cache has expired). A client a member connected is
never touched.

Run daily via the `oauth-client-prune` service in `.railway/railway.ts`.

Usage:
    python manage.py prune_oauth_clients
    python manage.py prune_oauth_clients --dry-run
"""

from __future__ import annotations

import logging

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.services import mcp_connections, mcp_oauth

log = logging.getLogger(__name__)


class Command(CronCommand):
    help = "Delete OAuth client registrations that nothing uses."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be deleted without deleting them.",
        )

    def handle(self, *args, **opts):
        days = mcp_oauth.client_prune_days()
        rows = mcp_connections.prunable_clients(timezone.now(), days)
        if opts["dry_run"]:
            self.stdout.write(
                f"[dry-run] would delete {rows.count()} unused clients older than {days} days."
            )
            return
        deleted = mcp_connections.prune_clients(rows)
        log.info("prune_oauth_clients: deleted=%d older_than_days=%d", deleted, days)
        self.stdout.write(f"Deleted {deleted} unused OAuth clients older than {days} days.")

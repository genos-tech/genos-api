"""`python manage.py export_team <team_id> --out <dir> --yes` - export one team's data on request (MIP-2257).

For a departing customer (DPA: an export window when the service stops). Operator only, no UI.
Writes JSON Lines plus the stored attachment files into `--out`; see `origin/services/team_export.py`.

* Refuses without `--yes`.
* Refuses in the production web container (`RAILWAY_SERVICE_NAME=backend-django`): a big export would
  starve the requests. Run it on a worker service or locally against a restore.
* Refuses an `--out` that already holds files, so two exports never mix.
* Writes a `SecurityEvent` (action export, source operator, target team) with the operator's OS user and the reason, and
  does it BEFORE any data is written: no audit row, no export.
* `--include-emails` adds member emails; by default members are names and roles only.
* Output on screen is counts only.

Runbook: genos-docs `operations/TEAM_EXPORT.md`.
"""

from __future__ import annotations

import getpass
import os
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.models.common.team_models import TeamMaster
from origin.services import team_export
from origin.services.security_events import record_security_event

WEB_SERVICE = "backend-django"


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class Command(BaseCommand):
    help = "Export one team's data and files to a directory (operator only; counts on screen)."

    def add_arguments(self, parser):
        parser.add_argument("team_id", help="The team's id (UUID).")
        parser.add_argument("--out", required=True, help="Empty or new directory to write into.")
        parser.add_argument("--yes", action="store_true", help="Confirm the export.")
        parser.add_argument("--reason", default="", help="Why (customer request); audited.")
        parser.add_argument("--include-emails", action="store_true", help="Add member emails.")

    def handle(self, *args, **opts):
        if os.environ.get("RAILWAY_SERVICE_NAME") == WEB_SERVICE:
            raise CommandError(
                "export_team must not run in the production web container: use a worker or a "
                "local restore."
            )
        if not opts["yes"]:
            raise CommandError("This copies a customer's data to disk: add --yes.")
        try:
            team_id = uuid.UUID(str(opts["team_id"]))
        except ValueError:
            raise CommandError("team_id must be a UUID.")
        if not TeamMaster._base_manager.filter(team_id=team_id).exists():
            raise CommandError("No such team.")
        out = Path(opts["out"])
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise CommandError("--out must be a new or empty directory.")

        event = record_security_event(
            SecurityAction.EXPORT,
            actor_label=f"operator:{_operator()}",
            force_source=SecuritySource.OPERATOR,
            team_id=team_id,
            target_type="team",
            target_id=str(team_id),
            metadata={
                "operator": _operator(),
                "reason": str(opts["reason"])[:1000],
                "include_emails": bool(opts["include_emails"]),
            },
        )
        if event is None:
            raise CommandError("The audit row could not be written, so nothing was exported.")

        manifest = team_export.export_team(
            team_id, out, include_emails=bool(opts["include_emails"])
        )
        self.stdout.write("export_team: done")
        for key, count in manifest["counts"].items():
            self.stdout.write(f"  {count} {key}")
        self.stdout.write(
            "  Send the archive over a secure channel, then delete it (operations/TEAM_EXPORT.md)."
        )

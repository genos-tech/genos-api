"""No-activity check (MIP-2022): has a connected AI app recorded nothing in Genos for days?

`GET /api/v2/mcp/connections/quiet/` answers the caller only, from data that already exists:

* **connected**: the member has a live (not revoked) `McpConnection`;
* **agent write**: a `TaskActivity` row (task changes and comments) or a `NoteVersionMaster` row
  the member's own session wrote with a `via` of a connected app (`oauth`) or an API key (`api_key`),
  the same stamp `agent_work` reads;
* **quiet**: connected, the oldest live connection is at least `QUIET_DAYS` old (a fresh one has had
  no time to record anything), no such write in the last `QUIET_DAYS` days, and the plan still
  includes MCP (a paused connection says so elsewhere and is not "broken").

Cheap by construction: two `EXISTS` queries bounded by the member's own `actor` / `editor` (the
foreign-key index) and a date, plus one connection read; the count does not grow with anything.
Nothing is written, no email, toast or Inbox item is made.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.search_engine.quota import get_mcp_enabled

QUIET_DAYS = 3
AGENT_VIA_KINDS = ["oauth", "api_key"]


def quiet_state(user, *, now=None, days: int = QUIET_DAYS) -> dict:
    now = now or timezone.now()
    since = now - timedelta(days=days)
    live = McpConnection.objects.filter(user=user, revoked_at__isnull=True).aggregate(
        oldest=Min("ts_created_at")
    )
    if live["oldest"] is None:
        return {"connected": False, "quiet": False, "days": days}

    task_write = TaskActivity.objects.filter(
        actor_id=user.id, ts_created_at__gte=since, metadata__via__kind__in=AGENT_VIA_KINDS
    ).exists()
    wrote = (
        task_write
        or NoteVersionMaster.objects.filter(
            editor_id=user.id, ts_created_at__gte=since, via__kind__in=AGENT_VIA_KINDS
        ).exists()
    )
    quiet = not wrote and live["oldest"] <= since and get_mcp_enabled(str(user.id))
    return {"connected": True, "quiet": quiet, "days": days}

"""Write and replay the ids-only erasure log (MIP-2249).

`record` is called by the four erasure paths inside their own transaction. `replay` re-runs the
same deletion services for every logged object that a backup restore brought back. Output is
COUNTS ONLY; nothing here logs an id.

The log lives in the database a restore replaces, so rows written AFTER the backup are lost with
the database. The runbook therefore exports the log from the live database BEFORE the swap
(`replay_erasures --export`) and replays from that file (`--from-file`).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from origin.models.common.erasure_models import RETENTION_DAYS, ErasureEvent, ErasureObjectType

logger = logging.getLogger(__name__)

# Accounts first (a team with no live member can then be purged), then teams, then what is left.
REPLAY_ORDER = (
    ErasureObjectType.ACCOUNT,
    ErasureObjectType.TEAM,
    ErasureObjectType.TASK,
    ErasureObjectType.MESSAGE,
)


def record(object_type: str, object_id) -> None:
    """Remember that this object was erased. Idempotent; id and time only."""
    ErasureEvent.objects.bulk_create(
        [ErasureEvent(object_type=object_type, object_id=str(object_id))],
        ignore_conflicts=True,
    )


def prune_before(now: datetime | None = None) -> datetime:
    return (now or timezone.now()) - timedelta(days=RETENTION_DAYS)


def export_events(since: datetime | None = None) -> list[dict]:
    """The log as plain dicts (type, id, time), for a file that outlives the database swap."""
    qs = ErasureEvent.objects.order_by("erased_at", "pk")
    if since is not None:
        qs = qs.filter(erased_at__gte=since)
    return [{"type": e.object_type, "id": e.object_id, "at": e.erased_at.isoformat()} for e in qs]


def load_events(path: str, since: datetime | None = None) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    out = []
    for row in rows:
        at = datetime.fromisoformat(row["at"])
        if since is None or at >= since:
            out.append({"type": row["type"], "id": str(row["id"]), "at": row["at"]})
    return out


# -- one replay function per object type; each returns "applied", "gone" or a "skipped: why" -----


def _replay_account(object_id: str, apply: bool) -> str:
    from origin.models.common.user_models import CustomUser
    from origin.services.account_deletion import OwnershipTransferRequired, delete_account

    user = CustomUser.objects.filter(pk=object_id, is_deleted=False).first()
    if user is None:
        return "gone"
    if not apply:
        return "applied"
    try:
        delete_account(user)
    except OwnershipTransferRequired:
        return "skipped: owns a team that has other members (transfer first)"
    return "applied"


def _replay_team(object_id: str, apply: bool) -> str:
    from origin.models.common.team_models import TeamMaster
    from origin.services import team_purge

    if not TeamMaster.objects.filter(team_id=object_id).exists():
        return "gone"
    if not apply:
        return "applied"
    # Never closes a team that still has a live member: the accounts were replayed first.
    team_purge.close_team_if_empty(object_id)
    outcome = team_purge.purge_team(object_id)
    if outcome.purged:
        return "applied"
    return "skipped: the team still has a live member or is held for billing"


def _replay_task(object_id: str, apply: bool) -> str:
    from origin.models.task.task_models import TaskMaster
    from origin.services import deleted_task_purge

    if not TaskMaster.objects.filter(task_id=object_id).exists():
        return "gone"
    if not apply:
        return "applied"
    reason = deleted_task_purge.purge_known_task(int(object_id))
    return "applied" if reason is None else f"skipped: {reason}"


def _replay_message(object_id: str, apply: bool) -> str:
    from origin.models.chat.unified_models import Message
    from origin.services import message_tombstone

    message = Message.objects.filter(pk=object_id).first()
    if message is None:
        return "gone"
    if message.deleted_at is not None and not message_tombstone.has_text(message):
        return "gone"
    if not apply:
        return "applied"
    with transaction.atomic():
        message_tombstone.tombstone_message(message)
    return "applied"


_REPLAYERS = {
    ErasureObjectType.ACCOUNT: _replay_account,
    ErasureObjectType.TEAM: _replay_team,
    ErasureObjectType.TASK: _replay_task,
    ErasureObjectType.MESSAGE: _replay_message,
}


def replay(events: list[dict], *, apply: bool) -> Counter:
    """Re-run the deletions for `events`. Dry run unless `apply`. Idempotent: an object that is
    already gone (or already blank) counts as `gone` and is not touched. One failing object is
    counted and does not stop the rest. Keys: `<type>: <outcome>`."""
    result: Counter = Counter()
    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)
    for object_type in REPLAY_ORDER:
        for event in by_type.get(object_type.value, []):
            try:
                outcome = _REPLAYERS[object_type](event["id"], apply)
            except Exception:  # noqa: BLE001 - one bad object must not stop the others; no id logged
                logger.warning("erasure replay: one %s failed", object_type.value, exc_info=False)
                outcome = "failed"
            if outcome == "applied" and not apply:
                outcome = "would erase"
            elif outcome == "applied":
                outcome = "erased"
            result[f"{object_type.value}: {outcome}"] += 1
    return result

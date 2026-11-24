"""Daily sweep of the agent-memory ledger (MIP-2251).

`MemberEvent` (180 days) and `MemberItemDay` (365 days) were pruned only when that member next
wrote a row (`member_events.record`, `item_activity._store`), so a member who stopped using the
app kept their rows for ever. This removes what is past its window for everyone, in small
batches. Counts only: no id, team or item is ever returned or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from origin.models.common.item_activity_models import MemberItemDay
from origin.models.common.member_event_models import MemberEvent
from origin.services import item_activity, member_events

BATCH = 2000


@dataclass
class SweepResult:
    events: int = 0
    item_days: int = 0


def _count_or_delete(queryset, *, apply: bool) -> int:
    if not apply:
        return queryset.count()
    total = 0
    while True:
        pks = list(queryset.values_list("pk", flat=True)[:BATCH])
        if not pks:
            return total
        deleted, _ = queryset.model.objects.filter(pk__in=pks).delete()
        total += deleted


def sweep(*, apply: bool, now: datetime | None = None) -> SweepResult:
    now = now or timezone.now()
    events = MemberEvent.objects.filter(ts__lt=now - member_events.RETENTION)
    # `day` is the member's local calendar day; one day of slack keeps a row whose zone is ahead of UTC.
    item_days = MemberItemDay.objects.filter(
        day__lt=(now - item_activity.RETENTION - timedelta(days=1)).date()
    )
    return SweepResult(
        events=_count_or_delete(events, apply=apply),
        item_days=_count_or_delete(item_days, apply=apply),
    )

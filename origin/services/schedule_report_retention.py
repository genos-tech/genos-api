"""How long a member's scheduled reports stay in their Inbox (MIP-1159).

A daily schedule files one report a day (`schedule_delivery`, each up to about 90 KB), and the
Inbox and the account export grow with them. A report is generated data: the job can make
another, and the schedule keeps its own `last_result`. So old ones are deleted, for real, by
the daily `prune_schedule_reports` cron.

## The rule

Reports are grouped by **(receiver, schedule)**: one member's reports of one schedule. A
report goes when EITHER of these holds:

1. it is **older than `REPORT_RETENTION_DAYS`** (30; a report exactly 30 days old stays), or
2. it is **below the newest `MAX_REPORTS_PER_SCHEDULE`** (60) of its group, newest first
   (ties broken by `item_id`), unless it is **unread, not deleted by the member and younger
   than the window**.

The second half is the protection: a report the member has not opened yet is never taken
within the window, even by the count. The newest report of a group is always in the newest 60,
so `schedule_delivery`'s once-per-run check, which looks at recent rows, never loses the row it
needs. Rows the member deleted (`is_deleted`) still count for the ranking and are no longer
protected.

Only `item_type` 10 is looked at. Digests (6), reminders (9), notices (0) and every request are
never selected, and another member's rows never count against a member's 60.

Nothing here writes anything but deletes, and a second run right after the first finds
nothing. `prune(dry_run=True)` counts without deleting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import F, Q, Window
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import RowNumber
from django.utils import timezone

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems

# The window: a report older than this many days is deleted.
REPORT_RETENTION_DAYS = 30
# The count: a schedule's reports beyond its newest this many are deleted.
MAX_REPORTS_PER_SCHEDULE = 60

_BATCH = 1_000


@dataclass
class PruneResult:
    expired: int  # older than the window
    over_cap: int  # beyond the newest MAX_REPORTS_PER_SCHEDULE (and not protected)
    deleted: int  # rows removed (0 on a dry run)
    dry_run: bool

    @property
    def matched(self) -> int:
        return self.expired + self.over_cap


def _ids_over_cap() -> list[int]:
    """Reports ranked below the newest MAX_REPORTS_PER_SCHEDULE of their (receiver, schedule).

    The ranking runs over EVERY report row (deleted ones too) and nothing else filters the
    inner query, so a row's rank never depends on the protection below."""
    ranked = InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT).annotate(
        _rank=Window(
            RowNumber(),
            partition_by=[
                F("receiver_id"),
                KeyTextTransform("schedule_id", "item_optionals"),
            ],
            order_by=[F("ts_created_at").desc(), F("item_id").desc()],
        )
    )
    return list(ranked.filter(_rank__gt=MAX_REPORTS_PER_SCHEDULE).values_list("item_id", flat=True))


def prune(*, now: datetime | None = None, dry_run: bool = False) -> PruneResult:
    now = now or timezone.now()
    cutoff = now - timedelta(days=REPORT_RETENTION_DAYS)
    reports = InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)

    expired = set(reports.filter(ts_created_at__lt=cutoff).values_list("item_id", flat=True))
    over_cap_ids = _ids_over_cap()
    # Unread, kept by the member and inside the window: never removed. (Past the window the
    # first rule has already taken it, so the protection only ever applies to the count.)
    over_cap = set(
        reports.filter(item_id__in=over_cap_ids)
        .exclude(Q(is_read=False, is_deleted=False, ts_created_at__gte=cutoff))
        .values_list("item_id", flat=True)
    )
    doomed = sorted(expired | over_cap)
    result = PruneResult(
        expired=len(expired),
        over_cap=len(over_cap - expired),
        deleted=0,
        dry_run=dry_run,
    )
    if dry_run:
        return result
    for start in range(0, len(doomed), _BATCH):
        batch = doomed[start : start + _BATCH]
        # `item_type` again: an id list is never trusted to still be a report.
        deleted, _ = InboxItems.objects.filter(
            item_id__in=batch, item_type=ITEM_TYPE_GENOS_REPORT
        ).delete()
        result.deleted += deleted
    return result

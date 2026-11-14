"""How long an AI-app notice is kept (MIP-1541).

An AI-app notice is an `Activity` row of `ActivityType.AI_APP` (`services/mcp_notice.py`): "Claude
Code created 15 tasks in QR", with up to five item titles in `meta`. The titles are the member's
own content and no job removed the rows, so they were kept for ever. They now expire.

## The rule

A notice is deleted once its `ts_created_at` is **older than `AI_APP_NOTICE_RETENTION_DAYS`**
(a notice exactly that old stays), whether it was read or not and whoever it is for. Only
`activity_type == AI_APP` is ever selected: every other kind of activity-feed row is untouched.

The same module is the ONE place the number lives: the command prints it, the tests read it and
the privacy text quotes it. Change the constant and nothing else.

Deleting is the whole job: a second run finds nothing, `prune(apply=False)` counts without
deleting, and a backlog goes in batches so one run never holds a long lock. The member's account
deletion (`account_deletion._delete_personal_data`) and the account export
(`account_export.build_export`) cover the same rows from the other two ends.

There is no expiry on read: comparable retention in this codebase (scheduled reports,
`schedule_report_retention`) is a daily command, and the feed's read path stays a plain indexed
query. A notice past the window may therefore show until the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import QuerySet
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType

# The window: a notice older than this many days is deleted. Ken, 2026-11-14 (MIP-1541).
AI_APP_NOTICE_RETENTION_DAYS = 90

_BATCH = 1_000


@dataclass
class PruneResult:
    matched: int  # notices past the window
    deleted: int  # rows removed (0 on a dry run)
    dry_run: bool


def expired(now: datetime | None = None) -> QuerySet:
    """The AI-app notices older than the window. Nothing else is ever in this queryset."""
    cutoff = (now or timezone.now()) - timedelta(days=AI_APP_NOTICE_RETENTION_DAYS)
    return Activity.objects.filter(activity_type=ActivityType.AI_APP, ts_created_at__lt=cutoff)


def prune(now: datetime | None = None, *, apply: bool = False) -> PruneResult:
    """Count (default) or delete (`apply=True`) the expired notices."""
    qs = expired(now)
    if not apply:
        return PruneResult(matched=qs.count(), deleted=0, dry_run=True)
    matched = 0
    deleted = 0
    while True:
        ids = list(qs.order_by("ts_created_at").values_list("pk", flat=True)[:_BATCH])
        if not ids:
            break
        matched += len(ids)
        _, per_model = Activity.objects.filter(
            pk__in=ids, activity_type=ActivityType.AI_APP
        ).delete()
        deleted += per_model.get(Activity._meta.label, 0)
        if len(ids) < _BATCH:
            break
    return PruneResult(matched=matched, deleted=deleted, dry_run=False)

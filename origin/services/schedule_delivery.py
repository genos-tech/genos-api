"""Where a schedule run's report goes (MIP-1147).

This step only keeps a one-line summary on the schedule (`last_result`) and logs it. The
Inbox step replaces `deliver` with the member's Inbox item (and the push, when asked); the
runner calls this one function and nothing else, so that swap touches this file alone.

`deliver` runs inside the run's try block: if it raises, the run FAILED (the member got
nothing), and the failure counts toward the auto-pause like a failing job.
"""

from __future__ import annotations

import logging

from origin.models.common.schedule_models import MemberSchedule
from origin.services.schedule_jobs import Report, item_count

log = logging.getLogger("origin.schedules")


def deliver(schedule: MemberSchedule, report: Report) -> None:
    result = {"title": str(report.get("title") or "")[:200], "items": item_count(report)}
    schedule.last_result = result
    # An UPDATE, not `save()`: a schedule the member deleted mid-run is simply gone, which
    # is not a failure of the run.
    MemberSchedule.objects.filter(pk=schedule.pk).update(last_result=result)
    # Ids and a count only: the title can quote what the member's data says.
    log.info(
        "schedule_delivered schedule=%s user=%s items=%d",
        schedule.id,
        schedule.user_id,
        result["items"],
    )

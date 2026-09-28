"""How to count task closes from `TaskActivity`.

`TaskActivityActionType.CLOSED` exists in the enum but **nothing emits
it**. Closing a task goes through the generic field-diff loop in
`origin/signals/task_signals.py`, which maps `status` to
`TaskActivityActionType.STATUS` and stores the new status in
`new_value` — so a close is recorded as
`action_type='status_changed', new_value='Closed'`, never
`action_type='closed'`.

Any query filtering on `action_type=CLOSED` therefore counts zero
forever. `TaskVelocityView` (`origin/views/task/task_activity_views.py`)
already derives closes the right way; this module is that same
predicate, shared, so the analytics tools can't drift from the chart
they're supposed to agree with.

Reopens still count correctly: reopening writes another STATUS row whose
`new_value` is an open status, so a close→reopen→close cycle yields two
matching rows — which is the property the throughput tools' docstrings
claim and the reason they read the activity log instead of
`TaskMaster.ts_updated_at`.

`new_value` is a `JSONField` holding a bare JSON string, so it compares
against plain Python strings (verified against real rows: 9 of 77 status
rows match `new_value__in=["Closed", "Deleted"]`).
"""

from __future__ import annotations

from django.db.models import Q

from origin.models.task.task_activity_models import TaskActivityActionType
from origin.services.task_blocking import CLOSED_STATUSES

# DELETED is a separate action_type with its own emitter (the
# `is_deleted` flag flip), so a soft-delete is NOT a close and is not
# folded in here. Only a status transition INTO a closed status counts.
CLOSE_Q = Q(
    action_type=TaskActivityActionType.STATUS.value,
    new_value__in=list(CLOSED_STATUSES),
)

CREATED_Q = Q(action_type=TaskActivityActionType.CREATED.value)

# What a throughput query scopes its base queryset to. Deliberately
# `CREATED_Q | CLOSE_Q` rather than `action_type__in=[CREATED, STATUS]`:
# the looser form would admit every non-close status change (WIP, Blocked,
# …) and those rows then surface in a bucketed response as spurious
# `created: 0, closed: 0` buckets on days when nothing was created or
# closed at all.
THROUGHPUT_Q = CREATED_Q | CLOSE_Q

"""One allocator for `TaskComments.comment_id` (MIP-1954).

`comment_id` is a per-task number guarded by `UniqueConstraint(task, comment_id)`. It used to be
allocated as the row COUNT + 1 in three places, so removing any row (a purge of deleted comments, a
force-delete) made the next comment re-use the id of a surviving one and collide. It is now
MAX(comment_id) + 1, taken while the task row is locked so two concurrent adds serialise instead of
reading the same maximum.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from origin.models import TaskComments, TaskMaster


def next_task_comment_id(task_id) -> int:
    """The next free `comment_id` for the task. Must run inside `transaction.atomic()`, and the
    comment must be inserted in that same transaction: the lock on the task row is held until it
    commits. The max counts deleted rows too (they keep their slot)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_task_comment_id must run inside transaction.atomic()")
    list(
        TaskMaster.objects.select_for_update().filter(task_id=task_id).values_list("pk", flat=True)
    )
    top = TaskComments.objects.filter(task_id=task_id).aggregate(m=Max("comment_id"))["m"]
    return (top or 0) + 1

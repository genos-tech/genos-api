"""Context-scoped switch to suppress TaskActivity audit inserts.

`origin.signals.task_signals._record` inserts a `TaskActivity` row from
`post_delete` receivers (COMMENT_DELETED, ATTACHMENT_REMOVED, …). During a
bulk teardown that deletes tasks in one transaction, those receivers fire
*while the task is being deleted* and insert an activity row that Django's
delete-collector has already finished collecting — so the new row escapes
the cascade and orphans. Django FK constraints are DEFERRABLE INITIALLY
DEFERRED, so the orphan isn't caught until COMMIT, which then raises
`IntegrityError: ... violates foreign key constraint
"origin_taskactivity_task_id_..._fk_origin_ta"`.

`suppress_task_activity()` makes `_record` a no-op for the duration of the
block. It uses a `ContextVar` (not `signal.disconnect`) on purpose: demo
teardown also runs inside a live web request (the LogoutView demo-signout
path), so globally disconnecting the receivers would drop audit rows for
other requests handled concurrently. A ContextVar is scoped to the current
execution context, so it only affects the teardown that set it. All the
delete signals fire synchronously in the same thread as the `.delete()`
call, so the flag set here is visible to them.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_suppress: ContextVar[bool] = ContextVar("suppress_task_activity", default=False)


def activity_suppressed() -> bool:
    """True while inside a `suppress_task_activity()` block."""
    return _suppress.get()


@contextmanager
def suppress_task_activity() -> Iterator[None]:
    """Skip TaskActivity audit inserts for the duration of the block.

    Always resets on exit — sync Django reuses worker threads across
    requests, so a leaked flag would silently stop auditing on whatever
    request the thread serves next.
    """
    token = _suppress.set(True)
    try:
        yield
    finally:
        _suppress.reset(token)

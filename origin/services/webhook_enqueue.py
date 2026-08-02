"""Putting events into the webhook outbox.

Mirrors `email_enqueue` in posture: **best-effort, post-commit, and it
never raises.** A webhook subscriber must not be able to break the write
that triggered it — an integration is an observer, and an observer that
can fail the observed action is a liability.

## Why this hangs off the task signal rather than `Activity`

The obvious hook is the `schedule_push_for_activities` fan-out, which
already reaches ~20 call sites. But `Activity` is **recipient-scoped**:
one row per person notified. A subscriber to `task.created` would get one
delivery per recipient of the same event and have to de-duplicate them,
which is a contract nobody wants to document.

The task signal is closer, and it already skips `is_init_task`
placeholder rows — the empty create-form draft — so integrators never see
a task that does not exist yet.

But it is **not** one signal per user action, which is the trap here.
Creating a task writes the row, then writes it again to set
`root_task_id`, and the milestone bridge can write it a third time. A
naive per-save hook therefore sent THREE webhooks for one thing
happening, two of them `task.updated` for a task that had just been
created. `_pending_for_transaction` collapses them.

## Delivery rows are created, not sent

Enqueue is a DB insert. `webhook_deliver_tick` does the sending, which
is what keeps a slow customer endpoint out of the request path.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.common.webhook_models import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_TASK_UPDATED,
    WebhookDelivery,
    WebhookEndpoint,
)

log = logging.getLogger("origin.webhooks")

# Statuses that mean "done". Mirrors the closed set used by the burndown
# and milestone views; kept as a literal here because the webhook event
# name is a public contract and should not silently change meaning when
# somebody adds an internal status.
CLOSED_STATUSES = {"Closed", "Deleted"}


def _iso(value):
    """Dates and datetimes as ISO 8601 strings.

    The payload is stored in a `JSONField` before it is ever sent, and
    Django's default encoder refuses a `datetime` — so coercing only at
    send time (where `json.dumps(default=str)` would have hidden it)
    fails at the INSERT instead. Doing it here also means the row in the
    outbox is byte-for-byte what the receiver gets, which is what makes
    a delivery log worth reading.
    """
    return value.isoformat() if hasattr(value, "isoformat") else value


def task_payload(task) -> dict:
    """The public shape of a task in a webhook body.

    Deliberately the same snake_case field names the public API returns,
    so an integrator who reads `/api/public/v1/tasks/` and one who
    receives `task.created` are looking at the same object.
    """
    return {
        "id": task.task_id,
        "display_id": task.display_id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "project_id": task.project_id,
        "team_id": str(task.team_id) if task.team_id else None,
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "reporter_id": str(task.reporter_id) if task.reporter_id else None,
        "due_date": _iso(task.due_date),
        "created_at": _iso(task.ts_created_at),
        "updated_at": _iso(task.ts_updated_at),
    }


def enqueue_event(team_id, event: str, payload: dict) -> None:
    """Queue `event` for every active endpoint in `team_id` that wants it.

    Best-effort: any failure is logged at WARNING and swallowed. WARNING
    rather than ERROR because this runs inside a request, and because
    `CronCommand` treats ERROR on the `origin` logger as a failed run.
    """
    try:
        endpoints = [
            e
            for e in WebhookEndpoint.objects.filter(team_id=team_id, is_active=True)
            if event in (e.events or [])
        ]
        if not endpoints:
            return
        WebhookDelivery.objects.bulk_create(
            [WebhookDelivery(endpoint=e, event=event, payload=payload) for e in endpoints]
        )
    except Exception:  # noqa: BLE001 — never break the caller's write
        log.warning("webhook enqueue failed for team=%s event=%s", team_id, event, exc_info=True)


# Most specific wins when one transaction produces several. Creating a
# task must not also announce that it was updated.
_EVENT_PRECEDENCE = {
    EVENT_TASK_CREATED: 3,
    EVENT_TASK_COMPLETED: 2,
    EVENT_TASK_UPDATED: 1,
}


def _pending_for_transaction() -> dict:
    """Per-transaction `{task_id: (team_id, event, payload)}`.

    ONE USER ACTION IS SEVERAL MODEL SAVES. Creating a task writes the
    row, then writes it again to set `root_task_id`, and the milestone
    bridge can write it a third time — so a naive per-save hook sends
    three webhooks for one thing happening, two of them `task.updated`
    for a task that was just created.

    Collapsing per transaction is the fix, and the transaction is the
    right boundary because it is exactly what "one user action" means
    here. The registry hangs off the DB connection so it is scoped
    correctly under concurrency and test isolation.

    **The rollback case is why this is not just a dict.** Django discards
    `on_commit` callbacks when a transaction rolls back, so the doomed
    task is never sent by its OWN callback — but a stale entry left on
    the connection would be picked up by the NEXT transaction's flush,
    announcing a task that never existed. An empty `run_on_commit` means
    the previous batch either ran (and cleared itself) or was discarded;
    either way anything still here is stale.
    """
    conn = transaction.get_connection()
    registry = getattr(conn, "_genos_webhook_pending", None)
    if registry is None:
        registry = {}
        conn._genos_webhook_pending = registry
    elif registry and not conn.run_on_commit:
        registry.clear()
    return registry


def _flush_one(registry: dict, task_pk) -> None:
    """Send exactly the entry this callback owns.

    Per task rather than draining the whole registry: a callback must
    never be able to deliver an entry belonging to a transaction that
    rolled back, and "flush everything pending" is precisely how that
    happens under nested atomics, where an inner rollback leaves outer
    callbacks queued.
    """
    entry = registry.pop(task_pk, None)
    if entry is None:
        return
    team_id, event, payload = entry
    enqueue_event(team_id, event, payload)


def schedule_task_event(task, *, created: bool, status_changed_to=None) -> None:
    """Queue the right task event AFTER the transaction commits.

    `on_commit` matters more here than for email: a webhook body carries
    the task's fields, and firing inside the transaction could deliver a
    state that then rolls back — leaving an integrator holding a task
    that never existed.

    One event per task per transaction, never one per save and never one
    per changed field.
    """
    if getattr(task, "is_init_task", False):
        return
    team_id = task.team_id
    if not team_id or task.pk is None:
        return

    if created:
        event = EVENT_TASK_CREATED
    elif status_changed_to is not None and status_changed_to in CLOSED_STATUSES:
        event = EVENT_TASK_COMPLETED
    else:
        event = EVENT_TASK_UPDATED

    registry = _pending_for_transaction()
    existing = registry.get(task.pk)
    if existing is not None:
        # Keep the more specific event, but always refresh the payload:
        # the later save is the more complete picture of the row.
        keep = existing[1]
        if _EVENT_PRECEDENCE.get(keep, 0) >= _EVENT_PRECEDENCE.get(event, 0):
            event = keep
    else:
        # Only the FIRST event for this TASK registers a callback, so it
        # runs once no matter how many saves follow — and it owns only
        # this task, so a rolled-back sibling can never ride along.
        task_pk = task.pk
        transaction.on_commit(lambda: _flush_one(registry, task_pk))

    registry[task.pk] = (team_id, event, task_payload(task))

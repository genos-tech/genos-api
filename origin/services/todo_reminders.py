"""To-do reminders: set one, cancel one, fire the due ones.

"Remind me about this to-do at 3pm" — the same promise `message_reminders`
makes about a message, made about a line on your list. A to-do is already a
note-to-self, but nothing about it comes back on its own: a list only helps
the person who goes and looks at it, and the whole reason something is on
tomorrow's list is that today you will not be thinking about it.

Two rules hold this together, both inherited from message reminders because
the alternative in each case is a reminder the user cannot trust:

1. **Finishing the to-do cancels the reminder.** Ticking it off retires any
   pending nudge (`cancel_pending`, called from `ToDoItemDetailView.patch`),
   and `_retire_if_moot` re-checks at the last possible moment in case the
   item was completed by some other path. A nudge about something already
   done is the kind of noise that gets a feature switched off. Deleting the
   to-do needs no such call — the FK cascades the reminder away with it.
2. **Firing is idempotent.** The inbox item and the `fired_at` stamp are
   written in one transaction, and the stamp doubles as the claim, so
   overlapping cron ticks cannot deliver twice and a crashed tick
   redelivers rather than loses.

One pending reminder per to-do (`uniq_pending_todo_reminder`): setting a
second time is a change of mind, not a second nudge. The superseded row is
cancelled rather than deleted, so what was asked for survives.

Delivery is Web Push (`schedule_push_for_reminder`, category
`todo_reminder`) plus an Inbox item in the Activities section — `item_type`
9, the same type a message reminder uses, discriminated by
`item_optionals["kind"]`. One type because it is the same *thing* from the
inbox's point of view: an activity, nothing awaiting a decision, not
counted toward the Requests badge. Both halves are the minutely cron's
work — see `management/commands/message_reminder_tick`, which drains both
kinds in one pass.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.chat.todo_models import ToDoItemReminder
from origin.models.common.inbox_models import InboxItems
from origin.services.reminder_window import (  # noqa: F401 — re-exported for the views
    MAX_HORIZON,
    PAST_TOLERANCE,
    ReminderWindowError,
    validate_remind_at,
)
from origin.services.reminder_window import warn_if_overdue as _warn_if_overdue
from origin.services.webpush_dispatch import schedule_push_for_reminder

logger = logging.getLogger(__name__)

# `InboxItems.item_type` — shared with message reminders on purpose; see the
# module docstring. `item_optionals["kind"]` tells the two apart.
ITEM_TYPE_REMINDER = 9

# `item_optionals["kind"]`, which is what the inbox card switches on to
# compose a to-do sentence instead of a message one.
REMINDER_KIND = "todo_reminder"

# Web push category, and the notification-settings key the user can turn
# off. Registered in `webpush_gating` under the `enable_inbox` master.
PUSH_CATEGORY = "todo_reminder"

# To-do title carried on the inbox card and the push body. Matches
# `message_reminders.PREVIEW_MAX` so a reminder reads the same either way.
PREVIEW_MAX = 140


def owner_of(item):
    """The user a to-do belongs to.

    `ToDoItem` has no `user` column — ownership is the group's, which is
    also why `ToDoItemReminder` doesn't duplicate it. One place spells the
    walk so a missing `select_related` shows up as slowness, not as a
    reminder delivered to nobody.
    """
    return item.group.user


def set_reminder(*, item, remind_at):
    """Schedule a reminder for `item`'s owner, replacing any pending one.

    Returns the reminder. Unlike the message flow there is no flag to
    couple to — the to-do IS the bookmark — so this only supersedes.
    """
    with transaction.atomic():
        ToDoItemReminder.objects.filter(
            item=item,
            fired_at__isnull=True,
            cancelled_at__isnull=True,
        ).update(cancelled_at=timezone.now())
        return ToDoItemReminder.objects.create(item=item, remind_at=remind_at)


def cancel_pending(*, item=None, item_ids=None, user=None) -> int:
    """Cancel pending reminder(s). Returns rows affected.

    Called when a to-do is completed (rule 1) as well as directly from the
    DELETE endpoint. Safe to call when there is nothing pending, which is
    the common case — most to-dos never carry a reminder, so the completion
    path must stay cheap.
    """
    qs = ToDoItemReminder.objects.filter(fired_at__isnull=True, cancelled_at__isnull=True)
    if item is not None:
        qs = qs.filter(item=item)
    if item_ids is not None:
        qs = qs.filter(item_id__in=item_ids)
    if user is not None:
        qs = qs.filter(item__group__user=user)
    return qs.update(cancelled_at=timezone.now())


def pending_for_user(user):
    """The user's outstanding to-do reminders, soonest first — what the
    client needs to show "reminder set for …" on a row it has in hand."""
    return (
        ToDoItemReminder.objects.filter(
            item__group__user=user,
            fired_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .select_related("item", "item__group")
        .order_by("remind_at")
    )


def warn_if_overdue(reminders, *, now=None) -> int:
    """The dead-cron canary over this user's pending to-do reminders. See
    `reminder_window.warn_if_overdue` for why this lives on a read path."""
    return _warn_if_overdue(reminders, label="todo-reminders", now=now, log=logger)


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= PREVIEW_MAX else text[: PREVIEW_MAX - 1].rstrip() + "…"


def preview_or_untitled(preview: str) -> str:
    """Push body for a to-do that has no title yet.

    A row can genuinely be blank — "+ New Todo" creates a placeholder the
    user is meant to type into — and an empty push body renders as a
    notification with a title and a void under it. Naming the state is more
    use than showing nothing.
    """
    return preview or "Untitled to-do"


def todo_href(item) -> str:
    """Deep link to the to-do, in the shape the frontend router understands
    (`parseInternalUrl` — `/workspace/todo/:localDate/item/:itemId`).

    Both segments earn their place: the date names the daily group the pane
    has to scroll to, and the item id is what highlights the row. It is the
    same URL the row's own "Copy link" writes, so the reminder lands exactly
    where the user would have sent themselves.
    """
    return f"/workspace/todo/{item.group.local_date.isoformat()}/item/{item.item_id}"


def _card(item, reminder) -> tuple[dict, dict]:
    """The inbox card's `(item_body, item_optionals)`.

    `item_body` carries an English `{title, text}` as the fallback for any
    client that can't read the optionals; the optionals carry the FACTS, so
    the client composes the sentence in the reader's own language. Same
    division as `message_reminders._card` — a reminder set in English still
    reads in Japanese.
    """
    preview = _truncate(item.title)
    body = {"title": "Reminder about your to-do", "text": preview}
    optionals = {
        "kind": REMINDER_KIND,
        "reminder_id": str(reminder.id),
        "todo_item_id": str(item.item_id),
        "todo_group_id": str(item.group_id),
        "local_date": item.group.local_date.isoformat(),
        # The card quotes the to-do under the same key the message card uses
        # for the message, so the preview block renders from one branch.
        "preview": preview,
        "href": todo_href(item),
        "remind_at": reminder.remind_at.isoformat(),
        "set_at": reminder.ts_created_at.isoformat(),
    }
    return body, optionals


def fire(reminder, *, now=None) -> bool:
    """Deliver one due reminder. True when it was delivered by this call.

    False means another tick got there first, or the to-do no longer has
    anything to remind about (see `_retire_if_moot`) — neither is an error.
    """
    now = now or timezone.now()
    if _retire_if_moot(reminder, now=now):
        return False

    item = reminder.item
    owner = owner_of(item)
    body, optionals = _card(item, reminder)
    with transaction.atomic():
        # The claim and the delivery in one transaction. The UPDATE's own
        # filter is the lock: a second pass that already lost the race
        # updates 0 rows and bails before writing an inbox item.
        claimed = ToDoItemReminder.objects.filter(
            pk=reminder.pk, fired_at__isnull=True, cancelled_at__isnull=True
        ).update(fired_at=now)
        if not claimed:
            return False
        inbox_item = InboxItems.objects.create(
            team_id=item.group.team_id,
            # Sender and receiver are the same person: a to-do reminder is
            # the user's own appointment with themselves, so there is no
            # actor but them. `request_status` stays blank — nothing here is
            # awaiting an answer.
            sender=owner,
            receiver=owner,
            item_type=ITEM_TYPE_REMINDER,
            item_body=body,
            item_optionals=optionals,
            request_status="",
        )
        schedule_push_for_reminder(
            recipient_id=owner.id,
            category=PUSH_CATEGORY,
            title=body["title"],
            body=preview_or_untitled(optionals["preview"]),
            url=optionals["href"],
            # Per reminder, not per to-do: two reminders on the same item
            # are two separate moments and must not collapse into one.
            tag=f"todo_reminder:{reminder.id}",
            actor=owner,
        )
    logger.info(
        "[todo-reminders] fired reminder=%s user=%s inbox_item=%s",
        reminder.id,
        owner.id,
        inbox_item.item_id,
    )
    return True


def _retire_if_moot(reminder, *, now) -> bool:
    """Cancel and report True when there is nothing left to remind about.

    One case: the to-do is done. (A deleted to-do takes its reminders with
    it through the FK cascade, so there is no row left to reach here.) The
    completion endpoint already cancels, so this is belt-and-braces — but a
    reminder about something the user has since finished is exactly the
    noise that gets notifications turned off, so it is worth re-checking at
    the last possible moment.
    """
    if not reminder.item.is_completed:
        return False
    ToDoItemReminder.objects.filter(
        pk=reminder.pk, fired_at__isnull=True, cancelled_at__isnull=True
    ).update(cancelled_at=now)
    logger.info("[todo-reminders] retired reminder=%s (to-do completed)", reminder.id)
    return True


def due_reminders(*, now=None, limit=200):
    """Pending reminders whose time has come, oldest due first (a backlog
    after an outage should come out in the order it was asked for)."""
    now = now or timezone.now()
    return list(
        ToDoItemReminder.objects.filter(
            fired_at__isnull=True,
            cancelled_at__isnull=True,
            remind_at__lte=now,
        )
        .select_related("item", "item__group", "item__group__user")
        .order_by("remind_at")[:limit]
    )


def fire_due(*, now=None, limit=200) -> dict:
    """Fire every due to-do reminder.

    Returns `{"fired", "skipped", "due", "max_late_seconds"}`.
    `max_late_seconds` is how far behind its time the oldest due reminder
    was — the number that says whether the tick is keeping up, which is the
    only health question this job has.

    One reminder's failure never stops the batch: the others belong to
    other people, and every one of them is late by definition.
    """
    now = now or timezone.now()
    due = due_reminders(now=now, limit=limit)
    fired = skipped = 0
    for reminder in due:
        try:
            if fire(reminder, now=now):
                fired += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 — one bad row must not stall the batch
            logger.exception("[todo-reminders] failed to fire reminder=%s", reminder.id)
    # `due` is ordered by `remind_at`, so the first row is the oldest.
    max_late = int((now - due[0].remind_at).total_seconds()) if due else 0
    return {
        "fired": fired,
        "skipped": skipped,
        "due": len(due),
        "max_late_seconds": max_late,
    }

"""Message reminders: set one, cancel one, fire the due ones.

"Remind me about this message in 3 hours" — the flag you can't forget.
`Flag` is a bookmark you have to remember to revisit; a reminder is the
same bookmark plus a time at which it comes back to you.

Three rules hold this together, and each exists because the alternative
is a reminder the user cannot trust:

1. **Setting a reminder flags the message.** When the reminder arrives,
   the thing it is about is already in the flagged list, which is where
   the user will look for it.
2. **Finishing with the flag cancels the reminder** — both ways of
   finishing (unflag, mark done). See `cancel_pending`.
3. **Firing is idempotent.** The inbox item and the `fired_at` stamp are
   written in one transaction, and the stamp doubles as the claim, so
   overlapping ticks cannot deliver the same reminder twice and a crashed
   tick redelivers rather than loses.

Delivery is Web Push (`schedule_push_for_reminder`) plus an Inbox item in
the Activities section (`item_type` 9). There is no chat-sidebar activity
row: that feed is what other people did to you.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.chat.unified_models import ChannelKind, Flag, MessageReminder
from origin.models.common.inbox_models import InboxItems

# Re-exported rather than defined here: the acceptance window and the
# dead-cron canary are shared with `todo_reminders` (see
# `reminder_window`), and every existing caller of these names imports
# them from this module.
from origin.services.reminder_window import (  # noqa: F401
    MAX_HORIZON,
    OVERDUE_WARN_AFTER,
    PAST_TOLERANCE,
    ReminderWindowError,
    validate_remind_at,
)
from origin.services.reminder_window import warn_if_overdue as _warn_if_overdue
from origin.services.webpush_dispatch import schedule_push_for_reminder

logger = logging.getLogger(__name__)

# `InboxItems.item_type` for a fired message reminder. Lives in the
# Activities half of the inbox (with 0 and 6), not Requests — nobody is
# waiting on an answer.
ITEM_TYPE_MESSAGE_REMINDER = 9

# Message preview carried on the inbox card and the push body. Matches
# `webpush_dispatch._PREVIEW_MAX` so a reminder reads like every other
# notification about a message.
PREVIEW_MAX = 140


def set_reminder(*, user, message, remind_at):
    """Schedule a reminder for `user` about `message`, and flag it.

    Replaces any pending reminder on the same message rather than adding a
    second one: "remind me in 1 hour" after "remind me tomorrow" is the
    user changing their mind, not asking for two nudges. The superseded row
    is cancelled, not deleted, so the history of what was asked for
    survives (and `uniq_pending_reminder` only ever sees one live row).

    Returns `(reminder, flag)`.
    """
    with transaction.atomic():
        MessageReminder.objects.filter(
            user=user,
            message=message,
            fired_at__isnull=True,
            cancelled_at__isnull=True,
        ).update(cancelled_at=timezone.now())

        flag, created = Flag.objects.get_or_create(user=user, message=message)
        # Asking to be reminded about something you had marked done
        # reopens it — the reminder is going to hand it back to you, so
        # leaving it in the past-flags list would be a contradiction.
        if not created and flag.completed_at is not None:
            flag.completed_at = None
            flag.save(update_fields=["completed_at"])

        reminder = MessageReminder.objects.create(user=user, message=message, remind_at=remind_at)
    return reminder, flag


def cancel_pending(*, user, message=None, message_ids=None) -> int:
    """Cancel the user's pending reminder(s). Returns rows affected.

    Called from the flag endpoints as well as directly: unflagging or
    completing a flag retires its reminder (rule 2 in the module
    docstring). Safe to call when there is nothing pending.
    """
    qs = MessageReminder.objects.filter(user=user, fired_at__isnull=True, cancelled_at__isnull=True)
    if message is not None:
        qs = qs.filter(message=message)
    if message_ids is not None:
        qs = qs.filter(message_id__in=message_ids)
    return qs.update(cancelled_at=timezone.now())


def pending_for_user(user):
    """The user's outstanding reminders, soonest first — what the client
    needs to show "reminder set for …" on a message it has in hand."""
    return (
        MessageReminder.objects.filter(user=user, fired_at__isnull=True, cancelled_at__isnull=True)
        .select_related("message")
        .order_by("remind_at")
    )


def warn_if_overdue(reminders, *, now=None) -> int:
    """The dead-cron canary over this user's pending message reminders.

    See `reminder_window.warn_if_overdue` — the whole rationale lives
    there, since a to-do reminder can go undelivered just as silently.
    """
    return _warn_if_overdue(reminders, label="reminders", now=now, log=logger)


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= PREVIEW_MAX else text[: PREVIEW_MAX - 1].rstrip() + "…"


def chat_href(message) -> str:
    """Deep link to the message, in the shape the frontend router
    understands (`parseInternalUrl` / `parseChatRoute`).

    Three segments, each earning its place: the channel; the thread when
    the message is a reply, since opening the parent chat leaves a reply
    out of sight; and the message itself, so the client scrolls to and
    highlights the bubble. Without that last one the link lands on the
    chat and the reader has to work out which message they asked to be
    reminded about — which is most of what the reminder was for.

    The message segment is the per-channel `seq`, the same id the in-app
    "Copy link to message" writes.
    """
    channel = message.channel
    try:
        token = ChannelKind(channel.kind).label
    except (ValueError, AttributeError):
        return "/workspace/chat"
    href = f"/workspace/chat/{token}/{channel.id}"
    root_id = message.thread_root_id or (message.parent_id if message.is_thread_reply else None)
    if root_id:
        href += f"/thread/{root_id}"
    if message.seq:
        href += f"/message/{message.seq}"
    return href


def _card(message, reminder) -> tuple[dict, dict]:
    """The inbox card's `(item_body, item_optionals)`.

    `item_body` carries an English `{title, text}` — the same shape the
    digest uses — as the fallback for any client that can't read the
    optionals. The optionals carry the FACTS (who, where, what, when
    asked), so the client can compose the sentence in the reader's own
    language and offer a link that actually opens the message.
    """
    preview = _truncate(message.body_text)
    sender_name = getattr(message.sender, "username", None) or ""
    chat_name = getattr(message.channel, "title", "") or ""
    body = {
        "title": (f"Reminder about {sender_name}'s message" if sender_name else "Reminder"),
        "text": preview,
    }
    optionals = {
        "kind": "message_reminder",
        "reminder_id": str(reminder.id),
        "message_id": str(message.id),
        "channel_id": str(message.channel_id),
        "chat_kind": message.channel.kind,
        "chat_name": chat_name,
        "thread_root_id": str(message.thread_root_id) if message.thread_root_id else "",
        "sender_name": sender_name,
        "preview": preview,
        "href": chat_href(message),
        "remind_at": reminder.remind_at.isoformat(),
        "set_at": reminder.ts_created_at.isoformat(),
    }
    return body, optionals


def fire(reminder, *, now=None) -> bool:
    """Deliver one due reminder. True when it was delivered by this call.

    False means somebody else's tick got there first, or the reminder no
    longer has anything to point at (see `_retire_if_moot`) — neither is
    an error.
    """
    now = now or timezone.now()
    if _retire_if_moot(reminder, now=now):
        return False

    message = reminder.message
    body, optionals = _card(message, reminder)
    with transaction.atomic():
        # The claim and the delivery in one transaction. The UPDATE's own
        # filter is the lock: a second pass that already lost the race
        # updates 0 rows and bails before writing an inbox item.
        claimed = MessageReminder.objects.filter(
            pk=reminder.pk, fired_at__isnull=True, cancelled_at__isnull=True
        ).update(fired_at=now)
        if not claimed:
            return False
        item = InboxItems.objects.create(
            team_id=message.channel.team_id,
            # The message's author, so the card and the push card icon show
            # whose message you asked to be reminded about. Unlike a request
            # item, nobody "sent" this — the sender is the subject, not an
            # asker, which is why `request_status` is blank.
            sender=message.sender,
            receiver=reminder.user,
            item_type=ITEM_TYPE_MESSAGE_REMINDER,
            item_body=body,
            item_optionals=optionals,
            request_status="",
        )
        schedule_push_for_reminder(
            recipient_id=reminder.user_id,
            title=body["title"],
            body=optionals["preview"],
            url=optionals["href"],
            # Per reminder, not per message: two reminders about the same
            # message are two separate moments and must not collapse.
            tag=f"message_reminder:{reminder.id}",
            actor=message.sender,
        )
    logger.info(
        "[reminders] fired reminder=%s user=%s inbox_item=%s",
        reminder.id,
        reminder.user_id,
        item.item_id,
    )
    return True


def _retire_if_moot(reminder, *, now) -> bool:
    """Cancel and report True when there is nothing left to remind about.

    Two cases, both of which mean the user already dealt with it:
    the message was deleted, or the flag is gone / marked done. The flag
    check is belt-and-braces — the flag endpoints cancel reminders
    directly — but a reminder that fires for a message the user has since
    filed away is the kind of noise that gets a feature turned off, so it
    is worth re-checking at the last possible moment.
    """
    message = reminder.message
    reason = None
    if message.deleted_at is not None:
        reason = "message deleted"
    elif not Flag.objects.filter(
        user_id=reminder.user_id, message_id=reminder.message_id, completed_at__isnull=True
    ).exists():
        reason = "no active flag"
    if reason is None:
        return False
    MessageReminder.objects.filter(
        pk=reminder.pk, fired_at__isnull=True, cancelled_at__isnull=True
    ).update(cancelled_at=now)
    logger.info("[reminders] retired reminder=%s (%s)", reminder.id, reason)
    return True


def due_reminders(*, now=None, limit=200):
    """Pending reminders whose time has come, oldest due first (a backlog
    after an outage should come out in the order it was asked for)."""
    now = now or timezone.now()
    return list(
        MessageReminder.objects.filter(
            fired_at__isnull=True,
            cancelled_at__isnull=True,
            remind_at__lte=now,
        )
        .select_related("message", "message__channel", "message__sender", "user")
        .order_by("remind_at")[:limit]
    )


def fire_due(*, now=None, limit=200) -> dict:
    """Fire every due reminder.

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
            logger.exception("[reminders] failed to fire reminder=%s", reminder.id)
    # `due` is ordered by `remind_at`, so the first row is the oldest.
    max_late = int((now - due[0].remind_at).total_seconds()) if due else 0
    return {
        "fired": fired,
        "skipped": skipped,
        "due": len(due),
        "max_late_seconds": max_late,
    }

"""Writes email-outbox rows at the same choke points that fan out Web Push.

The enqueue side of the email notification channel. Rows land in
`EmailNotificationEvent` (the outbox) and are drained by the
`email_notify_tick` cron, which does the away-gating, batching, and the
actual send. Everything here is best-effort post-commit — a failure to
enqueue must never fail the message/task/inbox write that triggered it,
mirroring the push posture.

Enqueue is gated on:
  - `settings.EMAIL_NOTIFICATIONS_ENABLED` (the series' dark-ship flag),
  - `should_email(recipient, category)` — so default-off categories
    (`chats`, `reactions`) never write dead rows,
  - `is_chat_muted` for the plain-message path (mirror of push).

`should_email` runs AGAIN at send time in the cron: this check is the
volume valve (don't fill the outbox with rows nobody wants), that one is
the correctness gate (prefs may change between enqueue and send).

This module is imported LAZILY from `webpush_dispatch`'s `schedule_*`
wrappers (which this module imports at top level for the shared spec
builders) — keep it that way or the two go circular.
"""

import logging

from django.conf import settings

from origin.models.chat.unified_models import ActivityType, ChannelKind
from origin.models.common.notification_models import EmailNotificationEvent
from origin.services.email_gating import should_email
from origin.services.webpush_dispatch import (
    _chat_url,
    _inbox_title,
    _push_spec,
    _task_url,
    _truncate,
)
from origin.services.webpush_gating import is_chat_muted

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(getattr(settings, "EMAIL_NOTIFICATIONS_ENABLED", False))


def _email_spec(act) -> dict | None:
    """Map an Activity to its email (category, title, body, url).

    Reuses `_push_spec` wholesale, plus the one branch push deliberately
    lacks: `ActivityType.TASK_ASSIGN`. Nothing PRODUCES task_assign rows
    yet (`create_self_assign_activity` deliberately emits MENTION so the
    legacy FE feed renders it) — this is forward wiring so the first real
    producer emails correctly without touching push behavior. Do NOT
    "unify" this into `_push_spec`: that would silently turn on a new
    push category.
    """
    if act.activity_type == ActivityType.TASK_ASSIGN:
        actor_name = getattr(act.actor, "username", None) or "Someone"
        return {
            "category": "task_assign",
            "title": f"{actor_name} assigned you a task",
            "body": (act.meta or {}).get("displayId", ""),
            "url": _task_url(act.meta),
        }
    return _push_spec(act)


def enqueue_email_for_activities(activities) -> None:
    """Outbox rows for freshly-committed Activity rows. Never raises."""
    if not _enabled() or not activities:
        return
    rows = []
    for act in activities:
        try:
            spec = _email_spec(act)
            if spec is None:
                continue
            recipient_id = str(act.recipient_id)
            if not should_email(recipient_id, spec["category"]):
                continue
            rows.append(
                EmailNotificationEvent(
                    user_id=recipient_id,
                    category=spec["category"],
                    title=spec["title"],
                    body=spec["body"] or "",
                    url=spec["url"] or "",
                    actor_name=getattr(act.actor, "username", "") or "",
                    activity_id=act.id,
                )
            )
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning(
                "[email] enqueue error for activity %s: %s", getattr(act, "id", "?"), exc
            )
    if not rows:
        return
    try:
        EmailNotificationEvent.objects.bulk_create(rows)
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning("[email] outbox bulk_create failed: %s", exc)


def enqueue_email_for_inbox_item(item, *, title: str | None = None) -> None:
    """Outbox row for the inbox item's receiver. Never raises. `title`
    overrides the item_type-derived default (same contract as the push
    dispatcher)."""
    if not _enabled() or item is None or not item.receiver_id:
        return
    try:
        recipient_id = str(item.receiver_id)
        if not should_email(recipient_id, "inbox"):
            return
        sender_name = getattr(item.sender, "username", None) or "Someone"
        EmailNotificationEvent.objects.create(
            user_id=recipient_id,
            category="inbox",
            title=title or _inbox_title(item, sender_name),
            body="",
            url="/workspace/inbox",
            actor_name=sender_name if item.sender_id else "",
            inbox_item=item,
        )
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning(
            "[email] inbox enqueue error for item %s: %s", getattr(item, "item_id", "?"), exc
        )


def enqueue_email_to_user(*, recipient_id, category, title, url) -> None:
    """Outbox row for a one-off notice with no Activity/InboxItems source
    (the `schedule_push_to_user` path). Both source FKs stay NULL, so the
    cron sends it without a read-state check. Never raises."""
    if not _enabled() or not recipient_id:
        return
    try:
        rid = str(recipient_id)
        if not should_email(rid, category):
            return
        EmailNotificationEvent.objects.create(
            user_id=rid,
            category=category,
            title=title,
            body="",
            url=url or "",
        )
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning("[email] user enqueue error for %s: %s", recipient_id, exc)


def enqueue_email_for_message(message, recipient_ids) -> None:
    """Outbox rows for a plain (non-mention) message. Gated by the `chats`
    category, which defaults OFF for email — so for most users this is a
    cheap no-op; only explicit `email:chats` opt-ins get rows. Honors the
    per-chat mute exactly like the push path. Never raises."""
    if not _enabled() or message is None or not recipient_ids:
        return
    try:
        channel = message.channel
        if channel is None:
            return
        sender_name = getattr(message.sender, "username", None) or "Someone"
        if channel.kind == ChannelKind.DM:
            title = sender_name
        else:
            title = f"{sender_name} in {getattr(channel, 'title', '') or 'a chat'}"
        body = _truncate(getattr(message, "body_text", ""))
        url = _chat_url(channel)
        rows = []
        for rid in recipient_ids:
            rid = str(rid)
            if is_chat_muted(rid, channel.id):
                continue
            if not should_email(rid, "chats"):
                continue
            rows.append(
                EmailNotificationEvent(
                    user_id=rid,
                    category="chats",
                    title=title,
                    body=body,
                    url=url,
                    actor_name=sender_name,
                )
            )
        if rows:
            EmailNotificationEvent.objects.bulk_create(rows)
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning("[email] message enqueue error for %s: %s", getattr(message, "id", "?"), exc)

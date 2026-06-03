"""Fans out Web Push for freshly-created Activity rows.

Called from every activity-creating path via `schedule_push_for_activities`
(which wraps `transaction.on_commit`, so a rollback never pushes a row
that didn't persist). For each eligible recipient it: derives a per-type
push spec (category / title / url), gates on preferences, skips anyone
with a visible tab (presence), looks up their active subscriptions,
builds a payload, and hands the slow HTTP send off to a thread pool so
the request isn't blocked.

Covered activity types: chat mentions, task-body / note surface mentions,
thread replies, task-comment replies, and reactions. Plain-`chats`
fan-out and full per-object mute parity land in later phases.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import transaction

from origin.models.chat.unified_models import ActivityType, ChannelKind
from origin.models.common.notification_models import PushSubscription
from origin.services import presence
from origin.services.v3_activity import (
    SURFACE_CHAT_NOTE,
    SURFACE_PERSONAL_NOTE,
    SURFACE_TASK_BODY,
    SURFACE_TASK_NOTE,
)
from origin.services.webpush_gating import should_push
from origin.services.webpush_sender import send_web_push, vapid_configured

logger = logging.getLogger(__name__)

# Small pool: the only thing offloaded is the per-subscription HTTP POST to
# the push service. Bounded so a slow push service can't spawn unbounded
# threads. (Productionization: move to an RQ worker on the existing Redis.)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webpush")

_PREVIEW_MAX = 140


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _PREVIEW_MAX else text[: _PREVIEW_MAX - 1].rstrip() + "…"


def _chat_url(channel) -> str:
    """Deep-link path the SW opens on click. Uses the same shape the FE
    router (`parseChatRoute` / `parseInternalUrl`) understands: the channel
    UUID + the kind token (dm/gm/pm/mdm)."""
    try:
        token = ChannelKind(channel.kind).label
    except (ValueError, AttributeError):
        return "/workspace/chat"
    return f"/workspace/chat/{token}/{channel.id}"


def _avatar_url(actor) -> str | None:
    """Absolute URL of the actor's avatar for the push card icon, or None
    (the SW then falls back to the app icon). Requires WEBPUSH_MEDIA_BASE_URL
    to be set — the server has no per-request host otherwise."""
    base = getattr(settings, "WEBPUSH_MEDIA_BASE_URL", "")
    fname = getattr(actor, "profile_image_file_name", "") or ""
    if base and fname:
        return f"{base.rstrip('/')}/{fname.lstrip('/')}"
    return None


def _task_url(meta: dict) -> str:
    """Deep-link for a task-body surface mention. Best-effort: a precise
    project/task link when `meta` carries both ids, else the task
    workspace root (the in-app activity row still routes precisely; the
    push is the away-nudge)."""
    project_id = (meta or {}).get("projectId")
    task_id = (meta or {}).get("taskId")
    if project_id and task_id:
        return f"/workspace/tasks/project/{project_id}/task/{task_id}"
    return "/workspace/tasks"


def _note_url(meta: dict, surface_type: int) -> str:
    """Deep-link for a note surface mention. Best-effort: a precise note
    link when `meta` carries the note id (+ routing for task / chat
    notes), else the notes workspace root."""
    meta = meta or {}
    note_id = meta.get("noteId")
    if surface_type == SURFACE_PERSONAL_NOTE and note_id:
        return f"/workspace/notes/my/{note_id}"
    if surface_type == SURFACE_TASK_NOTE and note_id and meta.get("projectId") and meta.get("taskId"):
        return (
            f"/workspace/notes/task/project/{meta['projectId']}"
            f"/task/{meta['taskId']}/note/{note_id}"
        )
    return "/workspace/notes"


def _push_spec(act) -> dict | None:
    """Map an Activity to its push (category, title, url); None = not a
    pushable type. Routing keys, in order:

      - REACTION                          -> reactions
      - THREAD_REPLY on a comment mirror  -> task_comments  (message.metadata.taskCommentId)
      - THREAD_REPLY otherwise            -> thread_replies
      - MENTION + surface task/task-note  -> mention_task
      - MENTION + surface personal/chat   -> mention_note
      - MENTION (channel-backed chat msg) -> mention_chat   (incl. comment @mentions, gated as mentions)
    """
    actor_name = getattr(act.actor, "username", None) or "Someone"
    channel = act.channel
    chat_url = _chat_url(channel) if channel else "/workspace/chat"
    body = _truncate(getattr(act.message, "body_text", "")) if act.message_id else ""
    atype = act.activity_type

    if atype == ActivityType.REACTION:
        emoji = (act.meta or {}).get("emoji", "")
        title = f"{actor_name} reacted {emoji}".strip()
        return {"category": "reactions", "title": title, "body": body, "url": chat_url}

    if atype == ActivityType.THREAD_REPLY:
        msg_meta = getattr(act.message, "metadata", None) or {}
        if msg_meta.get("taskCommentId"):
            return {
                "category": "task_comments",
                "title": f"{actor_name} commented on a task",
                "body": body,
                "url": chat_url,
            }
        return {
            "category": "thread_replies",
            "title": f"{actor_name} replied in a thread",
            "body": body,
            "url": chat_url,
        }

    if atype == ActivityType.MENTION:
        st = act.surface_type
        if st in (SURFACE_TASK_BODY, SURFACE_TASK_NOTE):
            return {
                "category": "mention_task",
                "title": f"{actor_name} mentioned you in a task",
                "body": (act.meta or {}).get("displayId", ""),
                "url": _task_url(act.meta) if st == SURFACE_TASK_BODY else _note_url(act.meta, st),
            }
        if st in (SURFACE_PERSONAL_NOTE, SURFACE_CHAT_NOTE):
            return {
                "category": "mention_note",
                "title": f"{actor_name} mentioned you in a note",
                "body": "",
                "url": _note_url(act.meta, st),
            }
        # Channel-backed chat-message mention (and self-assign, and
        # comment @mentions on the PM mirror — all gated as mentions).
        return {"category": "mention_chat", "title": f"{actor_name} mentioned you", "body": body, "url": chat_url}

    return None


def _queue_push(*, recipient_id, category, title, body, url, tag, actor=None) -> bool:
    """Core fan-out for ONE recipient: preference gate + presence skip +
    active-subscription lookup + queue the HTTP send. Returns True when at
    least one send was queued. The single place this gating lives so every
    push surface (activities, inbox, …) stays consistent — duplicating it
    per-path is what produced the original notification gaps.
    """
    recipient_id = str(recipient_id)
    if not should_push(recipient_id, category):
        return False
    # Don't push to someone actively looking at the app.
    if presence.has_visible_tab(recipient_id):
        return False
    subs = list(
        PushSubscription.objects.filter(user_id=recipient_id, is_active=True).values(
            "id", "endpoint", "p256dh", "auth"
        )
    )
    if not subs:
        return False
    payload = {
        "title": title,
        "body": body,
        "url": url,
        "tag": tag,
        # Card customizations (SW reads these). icon = sender avatar when
        # WEBPUSH_MEDIA_BASE_URL is set, else app icon.
        "icon": _avatar_url(actor),
        "requireInteraction": True,
        "actions": [{"action": "open", "title": "Open"}],
    }
    logger.info("[webpush] queue push user=%s category=%s subs=%d", recipient_id, category, len(subs))
    for sub in subs:
        _executor.submit(
            send_web_push,
            subscription_id=sub["id"],
            endpoint=sub["endpoint"],
            p256dh=sub["p256dh"],
            auth=sub["auth"],
            payload=payload,
        )
    return True


def dispatch_push_for_activities(activities) -> None:
    """Send a Web Push for each eligible activity. Never raises.

    Prefer `schedule_push_for_activities` from view code — it defers this
    to `transaction.on_commit` so a rolled-back request never pushes.
    """
    if not activities or not vapid_configured():
        return
    logger.debug(
        "[webpush] dispatch called: %d activities %s",
        len(activities),
        [(a.activity_type, str(getattr(a, "message_id", ""))[:8]) for a in activities],
    )
    for act in activities:
        try:
            spec = _push_spec(act)
            if spec is None:
                continue
            _queue_push(
                recipient_id=act.recipient_id,
                category=spec["category"],
                title=spec["title"],
                body=spec["body"],
                url=spec["url"],
                tag=f"{spec['category']}:{act.id}",
                actor=act.actor,
            )
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning(
                "[webpush] dispatch error for activity %s: %s",
                getattr(act, "id", "?"),
                exc,
            )


def schedule_push_for_activities(activities) -> None:
    """Defer a web-push fan-out for `activities` until the current DB
    transaction commits (so a rollback never pushes). Safe to call from
    any activity-creating view; no-op on an empty / falsy list.

    This is the single wiring point every activity producer should use —
    the gaps this fixes came from paths that created Activity rows but
    never reached dispatch.
    """
    acts = [a for a in (activities or []) if a is not None]
    if not acts:
        return
    transaction.on_commit(lambda: dispatch_push_for_activities(acts))


# item_type -> push title builder. Inbox items are their own surface (not
# the Activity feed), so they push under the `inbox` category (coarse
# `enable_inbox`). `approved` is a synthetic key for the GM-join approval
# notification sent back to the requester.
def _inbox_title(item, sender_name: str) -> str:
    titles = {
        0: f"{sender_name} sent you a message",
        1: f"{sender_name} asked to join your team",
        2: f"{sender_name} asked to join your project",
        3: f"{sender_name} asked to join your group chat",
    }
    return titles.get(item.item_type, "New inbox item")


def dispatch_push_for_inbox_item(item, *, title: str | None = None) -> None:
    """Web-push the inbox item's receiver. Never raises. `title` overrides
    the item_type-derived default (used for the GM-join approval notice)."""
    if item is None or not vapid_configured():
        return
    try:
        sender_name = getattr(item.sender, "username", None) or "Someone"
        _queue_push(
            recipient_id=item.receiver_id,
            category="inbox",
            title=title or _inbox_title(item, sender_name),
            body="",
            url="/workspace/inbox",
            tag=f"inbox:{item.item_id}",
            actor=item.sender,
        )
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning("[webpush] inbox dispatch error for item %s: %s", getattr(item, "item_id", "?"), exc)


def schedule_push_for_inbox_item(item, *, title: str | None = None) -> None:
    """Defer an inbox web-push until the current transaction commits."""
    if item is None:
        return
    transaction.on_commit(lambda: dispatch_push_for_inbox_item(item, title=title))


def schedule_push_to_user(*, recipient_id, category, title, url, actor=None, tag=None) -> None:
    """Defer a one-off web push to a single user not backed by an Activity
    or InboxItems row (e.g. the GM-join approval notice to the requester).
    Fires after commit; never raises."""
    if not recipient_id:
        return
    rid = str(recipient_id)
    resolved_tag = tag or f"{category}:{rid}"

    def _run():
        if not vapid_configured():
            return
        try:
            _queue_push(
                recipient_id=rid,
                category=category,
                title=title,
                body="",
                url=url,
                tag=resolved_tag,
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning("[webpush] user push error for %s: %s", rid, exc)

    transaction.on_commit(_run)

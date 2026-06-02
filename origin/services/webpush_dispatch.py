"""Fans out Web Push for freshly-created Activity rows.

Called from the message-create path via `transaction.on_commit` (so a
rollback never pushes a message that didn't persist). For each eligible
recipient it: gates on preferences, skips anyone with a visible tab
(presence), looks up their active subscriptions, builds a payload, and
hands the slow HTTP send off to a thread pool so the request isn't
blocked.

Slice scope: MENTION activities only. Thread-reply / task-comment /
plain-`chats` fan-out and the full mute parity land in later phases.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from origin.models.chat.unified_models import ActivityType, ChannelKind
from origin.models.common.notification_models import PushSubscription
from origin.services import presence
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


def dispatch_push_for_activities(activities) -> None:
    """Send a Web Push for each eligible MENTION activity. Never raises."""
    if not activities or not vapid_configured():
        return
    logger.debug(
        "[webpush] dispatch called: %d activities %s",
        len(activities),
        [(a.activity_type, str(getattr(a, "message_id", ""))[:8]) for a in activities],
    )
    for act in activities:
        try:
            # Slice scope: mentions only.
            if act.activity_type != ActivityType.MENTION:
                continue

            recipient_id = str(act.recipient_id)
            if not should_push(recipient_id, "mention_chat"):
                continue
            # Don't push to someone actively looking at the app.
            if presence.has_visible_tab(recipient_id):
                continue

            subs = list(
                PushSubscription.objects.filter(user_id=recipient_id, is_active=True).values(
                    "id", "endpoint", "p256dh", "auth"
                )
            )
            if not subs:
                continue

            actor_name = getattr(act.actor, "username", None) or "Someone"
            channel = act.channel
            payload = {
                "title": f"{actor_name} mentioned you",
                "body": _truncate(getattr(act.message, "body_text", "")),
                "url": _chat_url(channel) if channel else "/workspace/chat",
                "tag": f"mention:{act.id}",
                # Card customizations (SW reads these). icon = sender avatar
                # when WEBPUSH_MEDIA_BASE_URL is set, else app icon.
                "icon": _avatar_url(act.actor),
                "requireInteraction": True,
                "actions": [{"action": "open", "title": "Open"}],
            }

            logger.info(
                "[webpush] queue push user=%s activity=%s msg=%s subs=%d",
                recipient_id,
                str(act.id)[:8],
                str(getattr(act, "message_id", ""))[:8],
                len(subs),
            )
            for sub in subs:
                _executor.submit(
                    send_web_push,
                    subscription_id=sub["id"],
                    endpoint=sub["endpoint"],
                    p256dh=sub["p256dh"],
                    auth=sub["auth"],
                    payload=payload,
                )
        except Exception as exc:  # noqa: BLE001 — never break the caller
            logger.warning(
                "[webpush] dispatch error for activity %s: %s",
                getattr(act, "id", "?"),
                exc,
            )

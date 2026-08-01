"""Anonymous unsubscribe endpoints for the notification email channel.

Plain Django views, not DRF: the reader is logged out (they came from a
mail client), the responses are HTML pages, and the POST arrives either
from the confirm page's button or from a mail provider's RFC 8058
one-click flow — which sends no cookies, so CSRF is exempted and
authenticity rests entirely on the signed token (the `StripeWebhookView`
posture: no JWT, no CSRF, verify the credential in the request itself).

THE load-bearing rule: **GET must have no side effect.** Corporate mail
security prefetches every link in every email; a GET that unsubscribes
would silently unsubscribe entire companies. GET renders a confirm page;
only POST mutates. Keep it that way.
"""

import logging

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from origin.services.email_gating import _EMAIL_DEFAULTS
from origin.services.email_unsubscribe import SCOPE_ALL, SCOPE_DIGEST, parse_token

logger = logging.getLogger(__name__)

# Human-readable label per scope, for the confirm/done pages. Bilingual
# (ja operative + en), matching the landing site's legal pages — no
# locale plumbing needed for an anonymous reader.
_SCOPE_LABELS = {
    SCOPE_ALL: ("すべての通知メール", "all notification emails"),
    SCOPE_DIGEST: ("デイリーダイジェストメール", "the daily digest email"),
    "mention_chat": ("メンション通知メール", "mention notification emails"),
    "mention_thread": ("メンション通知メール", "mention notification emails"),
    "mention_task": ("メンション通知メール", "mention notification emails"),
    "mention_note": ("メンション通知メール", "mention notification emails"),
    "task_assign": ("タスク割り当て通知メール", "task assignment emails"),
    "thread_replies": ("スレッド返信通知メール", "thread reply emails"),
    "task_comments": ("タスクコメント通知メール", "task comment emails"),
    "inbox": ("リクエスト通知メール", "request notification emails"),
    "reactions": ("リアクション通知メール", "reaction emails"),
    "chats": ("チャット通知メール", "chat message emails"),
}


def _valid_scope(scope: str) -> bool:
    return scope == SCOPE_ALL or scope == SCOPE_DIGEST or scope in _EMAIL_DEFAULTS


def _resolve(token: str):
    """(user, scope) for a valid token whose user exists, else None."""
    from origin.models.common.user_models import CustomUser

    parsed = parse_token(token)
    if parsed is None:
        return None
    user_id, scope = parsed
    if not _valid_scope(scope):
        return None
    try:
        user = CustomUser.objects.filter(id=user_id).first()
    except (ValueError, TypeError):  # malformed uuid in a forged payload
        return None
    if user is None:
        return None
    return (user, scope)


def _apply(user, scope: str) -> None:
    """Idempotent single-key write. Deliberately NOT via the preferences
    serializer: that endpoint replaces the whole `category_settings` map,
    and racing a full-map PUT from an open settings tab would clobber
    this key (or vice versa). Read-modify-write of the one key only."""
    from origin.models.common.notification_models import NotificationPreference

    if scope == SCOPE_DIGEST:
        # The digest opt-out lives on the user row, not the prefs row.
        if user.email_digest_enabled:
            user.email_digest_enabled = False
            user.save(update_fields=["email_digest_enabled"])
        return
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    if scope == SCOPE_ALL:
        if prefs.email_enabled:
            prefs.email_enabled = False
            prefs.save(update_fields=["email_enabled", "ts_updated_at"])
        return
    key = f"email:{scope}"
    if prefs.category_settings.get(key) is not False:
        prefs.category_settings[key] = False
        prefs.save(update_fields=["category_settings", "ts_updated_at"])


def _page_context(scope: str, state: str) -> dict:
    ja, en = _SCOPE_LABELS.get(scope, _SCOPE_LABELS[SCOPE_ALL])
    return {"state": state, "scope_label_ja": ja, "scope_label_en": en}


@csrf_exempt
@require_http_methods(["GET", "POST"])
def email_unsubscribe(request, token: str):
    resolved = _resolve(token)
    if resolved is None:
        return render(
            request,
            "emails/unsubscribe_page.html",
            _page_context(SCOPE_ALL, "invalid"),
            status=400,
        )
    user, scope = resolved
    if request.method == "GET":
        # Confirm page ONLY — see the module docstring for why a GET must
        # never mutate.
        return render(request, "emails/unsubscribe_page.html", _page_context(scope, "confirm"))
    _apply(user, scope)
    logger.info("[email] unsubscribed user=%s scope=%s", user.pk, scope)
    return render(request, "emails/unsubscribe_page.html", _page_context(scope, "done"))

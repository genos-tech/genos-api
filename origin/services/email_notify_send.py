"""Renders and sends ONE batched notification email per user.

Consumed by the `email_notify_tick` cron. Takes a user plus their claimed
outbox rows, groups them by category, resolves the user's email locale,
prefixes `FRONTEND_BASE_URL` onto the rows' relative deep links, attaches
the List-Unsubscribe headers, and hands off to `send_templated_email`.

Locale layer: `resolve_email_locale` reads `CustomUser.language` (NULL →
English). A locale renders through `emails/<locale>/notification_batch.*`
when that pair exists, else falls back to the English root templates — a
missing translation must degrade, never 500 a user's batch.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.template.loader import get_template

from origin.services.email import send_templated_email
from origin.services.email_unsubscribe import unsubscribe_headers, unsubscribe_url

logger = logging.getLogger(__name__)

# Locales with their own template directory under emails/. English lives
# at the root as the universal fallback. Grow this set as translations
# land — nothing else needs touching.
_TEMPLATE_LOCALES = {"ja"}


def resolve_email_locale(user) -> str:
    lang = (getattr(user, "language", None) or "").strip().lower()
    return lang if lang in _TEMPLATE_LOCALES else "en"


# Category -> section heading, per locale. Keys mirror
# `email_gating._EMAIL_DEFAULTS`; an unknown category falls back to the
# "other" heading rather than dropping the row.
_HEADINGS = {
    "en": {
        "mention_chat": "Mentions",
        "mention_thread": "Mentions",
        "mention_task": "Mentions",
        "mention_note": "Mentions",
        "task_assign": "Assigned to you",
        "thread_replies": "Thread replies",
        "task_comments": "Task comments",
        "inbox": "Requests & notices",
        "reactions": "Reactions",
        "chats": "Messages",
        "_other": "Notifications",
    },
    "ja": {
        "mention_chat": "メンション",
        "mention_thread": "メンション",
        "mention_task": "メンション",
        "mention_note": "メンション",
        "task_assign": "あなたに割り当て",
        "thread_replies": "スレッド返信",
        "task_comments": "タスクコメント",
        "inbox": "リクエストと通知",
        "reactions": "リアクション",
        "chats": "メッセージ",
        "_other": "通知",
    },
}


def _absolute(url: str) -> str:
    if not url:
        return settings.FRONTEND_BASE_URL
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/{url.lstrip('/')}"


def _template_base(locale: str) -> str:
    """`"<locale>/notification_batch"` when both members of the pair
    exist, else the English root base."""
    if locale != "en":
        candidate = f"{locale}/notification_batch"
        try:
            get_template(f"emails/{candidate}.txt")
            get_template(f"emails/{candidate}.html")
            return candidate
        except TemplateDoesNotExist:
            logger.warning("[email] missing %s templates; falling back to en", locale)
    return "notification_batch"


def _subject(rows, locale: str) -> str:
    first = rows[0].title
    extra = len(rows) - 1
    if extra <= 0:
        return first
    if locale == "ja":
        return f"{first}（他{extra}件）"
    return f"{first} (+{extra} more)"


def _grouped(rows, locale: str) -> list[dict]:
    headings = _HEADINGS.get(locale, _HEADINGS["en"])
    groups: dict[str, dict] = {}
    for row in rows:
        label = headings.get(row.category, headings["_other"])
        group = groups.setdefault(label, {"label": label, "items": []})
        group["items"].append(
            {
                "title": row.title,
                "body": row.body,
                "url": _absolute(row.url),
            }
        )
    return list(groups.values())


def send_notification_batch(user, rows) -> None:
    """Render + send one email covering `rows`. Raises on transport
    failure — the cron owns retry bookkeeping (attempts / re-pending),
    so this function must NOT swallow errors."""
    locale = resolve_email_locale(user)
    context = {
        "user_name": user.username or "",
        "groups": _grouped(rows, locale),
        "total": len(rows),
        "app_url": settings.FRONTEND_BASE_URL,
        "unsubscribe_url": unsubscribe_url(user.id) or "",
    }
    send_templated_email(
        to=user.email,
        subject=_subject(rows, locale),
        template_base=_template_base(locale),
        context=context,
        headers=unsubscribe_headers(user.id),
    )

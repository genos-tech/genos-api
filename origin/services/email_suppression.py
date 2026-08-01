"""Suppression list for the notification email channel.

An address lands here when Resend reports a hard bounce or a spam
complaint (via Anymail's tracking webhook — see
`origin/signals/email_suppression_signals.py`), or when ops adds one
manually. The coalescer and digest senders check it before sending;
continuing to mail bounced addresses is the fastest way to burn the
Resend domain reputation that the transactional mails share.

Deliberately NOT consulted by the transactional paths (password reset,
verification, invite): a password reset must work even for a user who
unsubscribed or whose address once bounced — those sends are individually
requested, low-volume, and the reputational calculus is different.
"""

import logging

from origin.models.common.notification_models import EmailSuppression

logger = logging.getLogger(__name__)


def _normalize(address: str) -> str:
    return (address or "").strip().lower()


def is_suppressed(address: str) -> bool:
    normalized = _normalize(address)
    if not normalized:
        return True  # nowhere to send anyway
    return EmailSuppression.objects.filter(address=normalized).exists()


def suppress(address: str, reason: str) -> None:
    """Idempotent add. A complaint upgrades an existing bounce row (the
    stronger signal wins); nothing ever downgrades."""
    normalized = _normalize(address)
    if not normalized:
        return
    row, created = EmailSuppression.objects.get_or_create(
        address=normalized, defaults={"reason": reason}
    )
    if (
        not created
        and reason == EmailSuppression.REASON_COMPLAINT
        and row.reason != EmailSuppression.REASON_COMPLAINT
    ):
        row.reason = EmailSuppression.REASON_COMPLAINT
        row.save(update_fields=["reason"])
    logger.info("[email] suppressed %s (%s)", normalized, reason)

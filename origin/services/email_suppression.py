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

from django.db import IntegrityError, transaction

from origin.models.common.notification_models import EmailSuppression
from origin.services import blind_index

logger = logging.getLogger(__name__)


def _normalize(address: str) -> str:
    return (address or "").strip().lower()


def is_suppressed(address: str) -> bool:
    normalized = _normalize(address)
    if not normalized:
        return True  # nowhere to send anyway
    return EmailSuppression.objects.filter(
        blind_index.SUPPRESSED_ADDRESS.matches(normalized)
    ).exists()


def suppress(address: str, reason: str) -> None:
    """Idempotent add. A complaint upgrades an existing bounce row (the
    stronger signal wins); nothing ever downgrades."""
    normalized = _normalize(address)
    if not normalized:
        return
    # Not `get_or_create(address=...)`: the address is encrypted, so the
    # lookup goes through its hash, and a race into the unique hash
    # column is retried as the existing row.
    row = EmailSuppression.objects.filter(
        blind_index.SUPPRESSED_ADDRESS.matches(normalized)
    ).first()
    created = row is None
    if created:
        try:
            with transaction.atomic():
                row = EmailSuppression.objects.create(address=normalized, reason=reason)
        except IntegrityError:
            row = EmailSuppression.objects.get(blind_index.SUPPRESSED_ADDRESS.matches(normalized))
            created = False
    if (
        not created
        and reason == EmailSuppression.REASON_COMPLAINT
        and row.reason != EmailSuppression.REASON_COMPLAINT
    ):
        row.reason = EmailSuppression.REASON_COMPLAINT
        row.save(update_fields=["reason"])
    logger.info("[email] suppressed id=%s (%s)", row.pk, reason)

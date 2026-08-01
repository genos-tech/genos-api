"""Turns Anymail tracking events (Resend webhook) into suppressions.

Registered from `OriginConfig.ready` like the other signal modules. The
webhook itself is Anymail's `ResendTrackingWebhookView`, mounted under
`api/v2/email/anymail/` in urls.py and verified with the Svix signing
secret (`ANYMAIL_RESEND_SIGNING_SECRET`) — configure the webhook at
resend.com to send `email.bounced` and `email.complained` events.

Policy:
  * bounced    -> suppression row (stop notification email to the address)
  * complained -> suppression row AND `email_enabled=False` for every
    user on that address — a spam complaint is the user saying "stop",
    and honoring it only at the transport layer while the product keeps
    queueing mail for them would be answering the letter, not the intent.
"""

import logging

from anymail.signals import tracking
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(tracking, dispatch_uid="email_suppression_tracking")
def handle_tracking_event(sender, event, esp_name, **kwargs):
    # Lazy imports: this module is imported from AppConfig.ready, where
    # model imports are legal but keeping the surface minimal avoids
    # ready-time ordering surprises.
    from origin.models.common.notification_models import (
        EmailSuppression,
        NotificationPreference,
    )
    from origin.models.common.user_models import CustomUser
    from origin.services.email_suppression import suppress

    if event.event_type not in ("bounced", "complained"):
        return
    address = (event.recipient or "").strip().lower()
    if not address:
        return
    reason = (
        EmailSuppression.REASON_COMPLAINT
        if event.event_type == "complained"
        else EmailSuppression.REASON_BOUNCE
    )
    try:
        suppress(address, reason)
        if event.event_type == "complained":
            for user in CustomUser.objects.filter(email__iexact=address):
                prefs, _ = NotificationPreference.objects.get_or_create(user=user)
                if prefs.email_enabled:
                    prefs.email_enabled = False
                    prefs.save(update_fields=["email_enabled", "ts_updated_at"])
    except Exception:  # noqa: BLE001 — a handler bug must not 400 the webhook
        logger.exception("[email] suppression handler failed for %s", address)

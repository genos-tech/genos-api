"""POST /api/v2/contact/ — the public contact form (MIP-1261).

Anonymous, email-only: the message is mailed to support and is not stored
(see origin/services/contact_form.py). Throttled per IP and per sender address,
failing closed like the sign-in throttles.
"""

import logging

from django.utils.crypto import salted_hmac
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apis.request_id import current_request_id, describe_exception
from origin.services.contact_form import (
    ContactValidationError,
    send_contact_email,
    validate_submission,
)
from origin.services.throttles import AuthThrottle

logger = logging.getLogger(__name__)

# Field a bot fills and a person never sees (hidden with CSS, aria-hidden).
HONEYPOT_FIELD = "website"


class ContactIpThrottle(AuthThrottle):
    """Caps how many messages one address can send in an hour."""

    rate = "20/hour"
    scope = "contact_ip"


class ContactEmailThrottle(AuthThrottle):
    """Caps how many messages one sender address can send, whatever the IP.

    The key is a keyed hash of the address, not the address itself, so the
    rate-limit cache never holds the plain address.
    """

    rate = "5/hour"
    scope = "contact_email"

    def get_cache_key(self, request, view):
        data = request.data
        raw = data.get("email") if hasattr(data, "get") else None
        if not isinstance(raw, str) or not raw.strip():
            # Nothing to key on: the view rejects the request, the IP limit still applies.
            return None
        digest = salted_hmac("origin.contact_email", raw.strip().lower(), algorithm="sha256")
        return self.cache_format % {"scope": self.scope, "ident": digest.hexdigest()}


class ContactView(APIView):
    """Accept a contact-form message and mail it to support.

    202 on success with no echo of the data. 400 with field codes only when a
    field is invalid. 503 when the mail could not be sent, so the form can offer
    the plain mailto fallback.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ContactIpThrottle, ContactEmailThrottle]

    def post(self, request):
        data = request.data if hasattr(request.data, "get") else {}
        request_ref = current_request_id() or "-"

        if data.get(HONEYPOT_FIELD):
            # A bot filled the hidden field: report success, send nothing.
            logger.info("contact.honeypot request=%s", request_ref)
            return Response({"accepted": True}, status=status.HTTP_202_ACCEPTED)

        try:
            submission = validate_submission(data)
        except ContactValidationError as exc:
            return Response({"errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)

        try:
            send_contact_email(submission)
        except Exception as exc:
            # The type only: a mail error's text can quote the sender's address.
            logger.error(
                "contact.send_failed topic=%s request=%s: %s",
                submission.topic,
                request_ref,
                describe_exception(exc),
            )
            return Response({"error": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        logger.info("contact.sent topic=%s request=%s", submission.topic, request_ref)
        return Response({"accepted": True}, status=status.HTTP_202_ACCEPTED)

"""Public contact form: validate what a visitor typed and mail it to support.

Data minimisation (MIP-1261, decision D11): the message is sent by email to
the support inbox and is NOT stored. Nothing in this module writes to the
database, a log line, or an analytics service. The log line in the view
carries the topic and a short request id only; the sender's name, address and
message never reach a log (see the log-minimisation rules of genos-api #995).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email

# The address the form mails. Read from settings when set (so an operator can
# change it without a deploy), else the public support inbox the /support page
# already prints. Not defined in apis/settings.py on purpose: that file is not
# edited by this change.
DEFAULT_SUPPORT_EMAIL = "genos.support@genosai.dev"

TOPICS = ("general", "enterprise", "security", "press")
LANGUAGES = ("en", "ja")

MAX_NAME = 200
MAX_MESSAGE = 4000
MAX_EMAIL = 254

# Control characters except tab and newline (both legitimate in a message).
# CR is dropped too; line endings are normalised to LF before this runs.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_TOPIC_LABEL = {
    "general": "General",
    "enterprise": "Enterprise enquiry",
    "security": "Security",
    "press": "Press",
}


class ContactValidationError(ValueError):
    """Carries a {field: code} map; the view turns it into a 400."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(", ".join(sorted(errors)))
        self.errors = errors


@dataclass(frozen=True)
class ContactSubmission:
    name: str
    email: str
    topic: str
    message: str
    language: str


def clean_text(value: str) -> str:
    """Normalise line endings, drop control characters, trim the ends."""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL.sub("", value)
    return value.strip()


def _clean_single_line(value: str) -> str:
    # Names and addresses are one line: newlines are removed, not kept, so a
    # value can never start a new header.
    return " ".join(clean_text(value).split())


def validate_submission(data) -> ContactSubmission:
    """Validate and clean the posted fields. The honeypot is checked by the
    view before this runs; it is not a field here."""
    errors: dict[str, str] = {}

    fields = data if hasattr(data, "get") else {}
    raw_name = fields.get("name")
    raw_email = fields.get("email")
    raw_topic = fields.get("topic")
    raw_message = fields.get("message")
    raw_language = fields.get("language")

    name = _clean_single_line(raw_name) if isinstance(raw_name, str) else ""
    if not name:
        errors["name"] = "required"
    elif len(name) > MAX_NAME:
        errors["name"] = "too_long"

    email = _clean_single_line(raw_email) if isinstance(raw_email, str) else ""
    if not email:
        errors["email"] = "required"
    elif len(email) > MAX_EMAIL:
        errors["email"] = "invalid"
    else:
        try:
            validate_email(email)
        except DjangoValidationError:
            errors["email"] = "invalid"

    topic = raw_topic if isinstance(raw_topic, str) else ""
    if topic not in TOPICS:
        errors["topic"] = "invalid"

    message = clean_text(raw_message) if isinstance(raw_message, str) else ""
    if not message:
        errors["message"] = "required"
    elif len(message) > MAX_MESSAGE:
        errors["message"] = "too_long"

    language = raw_language if isinstance(raw_language, str) else ""
    if language not in LANGUAGES:
        language = "en"

    if errors:
        raise ContactValidationError(errors)
    return ContactSubmission(
        name=name, email=email, topic=topic, message=message, language=language
    )


def support_address() -> str:
    return getattr(settings, "SUPPORT_EMAIL", DEFAULT_SUPPORT_EMAIL)


def send_contact_email(submission: ContactSubmission) -> None:
    """Send ONE plain-text email to support, Reply-To the sender.

    Raises on transport failure; the caller logs the exception type only,
    because a mail error's text can quote the address (MIP-622).
    """
    body = (
        f"Topic: {_TOPIC_LABEL[submission.topic]}\n"
        f"Page language: {submission.language}\n"
        f"From: {submission.name} <{submission.email}>\n"
        "\n"
        f"{submission.message}\n"
    )
    msg = EmailMessage(
        subject=f"[Genos contact] {_TOPIC_LABEL[submission.topic]}",
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[support_address()],
        reply_to=[submission.email],
    )
    msg.send(fail_silently=False)

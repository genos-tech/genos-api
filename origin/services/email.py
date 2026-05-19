"""Outgoing email helpers.

Rendering uses Django's template loader so plain-text and HTML bodies
can live under `origin/templates/emails/`. Apps with `APP_DIRS=True`
(see TEMPLATES in settings.py) auto-discover this directory.

The actual transport — console in dev, Gmail SMTP in prod — is picked
by `EMAIL_BACKEND` in settings; this helper is transport-agnostic.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_templated_email(*, to: str, subject: str, template_base: str, context: dict) -> None:
    """Send a multipart text/HTML email rendered from a pair of templates.

    `template_base="password_reset"` reads
    `templates/emails/password_reset.txt` and `.html`. Both must exist
    so clients that strip HTML still see something useful.
    """
    text_body = render_to_string(f"emails/{template_base}.txt", context)
    html_body = render_to_string(f"emails/{template_base}.html", context)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)

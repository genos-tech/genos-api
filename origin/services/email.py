"""Outgoing email helpers.

Rendering uses Django's template loader so plain-text and HTML bodies
can live under `origin/templates/emails/`. Apps with `APP_DIRS=True`
(see TEMPLATES in settings.py) auto-discover this directory.

The actual transport — console in dev; in prod Resend's HTTPS API via
django-anymail, because Railway blocks outbound SMTP (see the Email
section in settings.py) — is picked by `EMAIL_BACKEND`; this helper is
transport-agnostic.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import Context
from django.template.loader import get_template, render_to_string


def _render_text(name: str, context: dict) -> str:
    """Render a `.txt` template with autoescape OFF.

    Django autoescapes every template, `.txt` included, so a name or title with
    a quote, `&` or `<` reached the plain-text part as `O&#x27;Brien &amp; Co`
    (MIP-787). The text part is `text/plain` and is never interpreted as HTML,
    so escaping can only corrupt it; and a second `&` in a link would have
    broken that link. The HTML part is untouched and stays escaped.

    Done here, in the one place every email passes through, so a new template
    (or a locale's) cannot forget it. `{% extends %}` and `{% include %}` run
    under the same context, so `base.txt` and the footer are covered too.
    """
    return get_template(name).template.render(Context(context, autoescape=False))


def send_templated_email(
    *,
    to: str,
    subject: str,
    template_base: str,
    context: dict,
    headers: dict | None = None,
) -> None:
    """Send a multipart text/HTML email rendered from a pair of templates.

    `template_base="password_reset"` reads
    `templates/emails/password_reset.txt` and `.html`. Both must exist
    so clients that strip HTML still see something useful.

    `headers` adds raw message headers (the notification channel uses it
    for `List-Unsubscribe`); transactional callers omit it.
    """
    text_body = _render_text(f"emails/{template_base}.txt", context)
    html_body = render_to_string(f"emails/{template_base}.html", context)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to],
        headers=headers or None,
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)

"""Deploy-time system checks for configuration that fails SILENTLY.

Django runs these on every management command (including the email
crons), so a misconfiguration surfaces in the deploy/cron logs instead
of being discovered from a user complaint.

Scope rule: only add a check here when the wrong value produces no
error — something that runs green and delivers a broken result. Config
that crashes on use doesn't need a check; it already tells you.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.checks import Error, Warning, register


def _normalized(name: str) -> str:
    return (getattr(settings, name, "") or "").strip().rstrip("/")


def _host(value: str) -> str:
    """Host portion, scheme and trailing slash removed — so
    `api.example.com` and `https://api.example.com` compare equal."""
    return value.split("://", 1)[-1].rstrip("/")


@register()
def email_public_url_check(app_configs, **kwargs):
    """`API_PUBLIC_BASE_URL` must be the API's own host.

    Unsubscribe links are built from it and must reach Django. Pointing
    it at the FRONTEND host is the trap this check exists for: an SPA
    host answers every path with 200 + index.html, so the link "works"
    — it just opens the app instead of the unsubscribe page, and the
    RFC 8058 one-click POST that mailbox providers send never reaches
    the endpoint. Nothing errors; deliverability quietly suffers.

    Warnings, not Errors, on purpose: a broken unsubscribe link must not
    stop notification email from being sent at all.
    """
    if not getattr(settings, "EMAIL_NOTIFICATIONS_ENABLED", False):
        return []

    api = _normalized("API_PUBLIC_BASE_URL")
    frontend = _normalized("FRONTEND_BASE_URL")
    issues = []

    if not api:
        issues.append(
            Warning(
                "API_PUBLIC_BASE_URL is unset while the email notification channel is enabled.",
                hint=(
                    "Notification emails will ship with NO unsubscribe link "
                    "and no List-Unsubscribe header. Set it to this API's "
                    "public origin (e.g. https://api.example.com)."
                ),
                id="origin.W001",
            )
        )
        return issues

    if "://" not in api:
        issues.append(
            Warning(
                f"API_PUBLIC_BASE_URL has no scheme ({api!r}); unsubscribe "
                "links would be relative.",
                hint=(
                    "A bare host makes mail clients resolve the link against "
                    "the message instead of the web (Apple Mail shows "
                    "'no application set to open x-webdoc://…', and one-click "
                    "unsubscribe never leaves). https:// is assumed at send "
                    "time so links still work — set it explicitly, e.g. "
                    "https://api.example.com."
                ),
                id="origin.W003",
            )
        )

    # Compared scheme-insensitively so a bare frontend host trips this
    # too, not just W003 above.
    if _host(api) and _host(api) == _host(frontend):
        issues.append(
            Warning(
                "API_PUBLIC_BASE_URL equals FRONTEND_BASE_URL "
                f"({api!r}); unsubscribe links will not reach Django.",
                hint=(
                    "The frontend host serves the SPA for every path, so the "
                    "link opens the app instead of the unsubscribe page and "
                    "one-click unsubscribe fails. Point API_PUBLIC_BASE_URL "
                    "at the API host (e.g. https://api.example.com) — the "
                    "host that serves /api/v2/email/unsubscribe/."
                ),
                id="origin.W002",
            )
        )

    return issues


@register()
def username_field_unique_check(app_configs, **kwargs):
    """Sign-in's address must be unique, through its blind index.

    Stands in for Django's auth.E003, silenced in settings: `email` is
    encrypted, so the column can't carry the constraint (MIP-619). Without
    a unique hash, two accounts could share an address, and sign-in
    (`get_by_natural_key`) would refuse both.
    """
    user = get_user_model()
    index = next(
        (i for i in getattr(user, "blind_indexes", ()) if i.source == user.USERNAME_FIELD),
        None,
    )
    if index is not None and user._meta.get_field(index.column).unique:
        return []
    return [
        Error(
            f"{user._meta.label}.{user.USERNAME_FIELD} has no unique blind index.",
            hint=(
                "auth.E003 is silenced because the column is encrypted, so "
                f"list a BlindIndex on {user.USERNAME_FIELD!r} in "
                "`blind_indexes` and make its hash column unique."
            ),
            id="origin.E001",
        )
    ]

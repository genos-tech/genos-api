"""Signed unsubscribe tokens and the List-Unsubscribe header pair.

Every notification/digest email must carry a working unsubscribe path
for an anonymous, logged-out reader (the recipient is by definition not
in the app). The token is a `django.core.signing` payload — stateless,
verified by SECRET_KEY — carrying the user id and a scope:

  - "all"                → flip `NotificationPreference.email_enabled` off
  - a fine category key  → write `category_settings["email:<cat>"] = False`
  - "digest"             → reserved; wired when the email digest lands
                           (`CustomUser.email_digest_enabled`, PR A6)

Deliberately NO max_age: an unsubscribe link that stops working is a
compliance failure, and the action is benign and idempotent. The header
token is always all-scope — a mail client's own Unsubscribe button means
"stop emailing me", not "stop this category".

SECRET_KEY must be identical on every service that mints or verifies
these (backend-django and the email crons), or footer links 400.
"""

from django.conf import settings
from django.core import signing

_SALT = "email-unsub"

SCOPE_ALL = "all"
SCOPE_DIGEST = "digest"


def make_token(user_id, scope: str) -> str:
    return signing.dumps({"u": str(user_id), "s": str(scope)}, salt=_SALT)


def parse_token(token: str) -> tuple[str, str] | None:
    """`(user_id, scope)`, or None for a tampered/undecodable token."""
    try:
        data = signing.loads(token, salt=_SALT)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    user_id = data.get("u")
    scope = data.get("s")
    if not user_id or not scope or not isinstance(scope, str):
        return None
    return (str(user_id), scope)


def unsubscribe_url(user_id, scope: str = SCOPE_ALL) -> str | None:
    """Absolute unsubscribe URL, or None when API_PUBLIC_BASE_URL is
    unset (emails then simply omit the footer link/headers rather than
    emitting a broken relative URL)."""
    base = getattr(settings, "API_PUBLIC_BASE_URL", "") or ""
    if not base:
        return None
    return f"{base.rstrip('/')}/api/v2/email/unsubscribe/{make_token(user_id, scope)}/"


def unsubscribe_headers(user_id) -> dict:
    """`List-Unsubscribe` + RFC 8058 one-click headers for a message to
    `user_id`, or {} when no public base URL is configured. Pass to
    `send_templated_email(headers=...)`."""
    url = unsubscribe_url(user_id, SCOPE_ALL)
    if not url:
        return {}
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }

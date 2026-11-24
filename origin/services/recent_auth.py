"""A recent password check, asked for before a lasting foothold is made (MIP-2240).

A stolen 24 h access token must not be enough to make something that outlives the victim's
password reset: an API key, a webhook endpoint, a connected AI app. So those three creations
ask for the password again. The member types it into `POST /api/v2/user/confirm-password/`,
which answers a short-lived signed token; the creating request carries it as
`recent_auth_token`.

The token is stateless (no table, no cache entry to lose). It names the user and a fragment of
their CURRENT password hash and expires after `WINDOW_SECONDS`, so a password change or reset
makes every token still in a browser worthless, and a token cannot be carried to another
account. It proves "the password was typed in the last ten minutes by whoever holds this
token", which is exactly the claim a thief with only the access token cannot make.

An account with no usable password (sign-in through Google or GitHub) has nothing to type, so
the gate does not apply to it; the owner email (`credential_alert`) still goes out. A second
factor for those accounts is MIP-2239.
"""

from __future__ import annotations

import logging

from django.core import signing
from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 600
# The Nth wrong password in an hour raises one alert event and emails the owner (MIP-2290).
ALERT_AFTER_FAILURES = 5
_FAILURE_WINDOW = 3600
_SALT = "genos.recent-auth.v1"
CODE = "recent_auth_required"


def _fingerprint(user) -> str:
    """Django's own session-auth hash: a keyed HMAC of the current password hash. It changes when
    the password does, and reveals nothing about the hash."""
    return user.get_session_auth_hash()[:24]


def required_for(user) -> bool:
    """Whether this account has a password to ask for."""
    return bool(user.has_usable_password())


def issue(user) -> str:
    """A token for `user`, to be handed out only after their password was checked."""
    return signing.dumps({"u": str(user.pk), "f": _fingerprint(user)}, salt=_SALT)


def is_valid(user, token) -> bool:
    if not isinstance(token, str) or not token or len(token) > 512:
        return False
    try:
        data = signing.loads(token, salt=_SALT, max_age=WINDOW_SECONDS)
    except signing.BadSignature:  # includes SignatureExpired
        return False
    return (
        isinstance(data, dict)
        and data.get("u") == str(user.pk)
        and data.get("f") == _fingerprint(user)
    )


def refusal(request) -> Response | None:
    """`None` when the request may go on; otherwise the 403 the pages answer with the password
    prompt. Read from the body (`recent_auth_token`): the web app already sends JSON bodies on
    these three calls, and a header would need a CORS change."""
    user = request.user
    if not required_for(user):
        return None
    data = request.data if isinstance(request.data, dict) or hasattr(request.data, "get") else {}
    if is_valid(user, data.get("recent_auth_token")):
        return None
    return Response(
        {
            "error": "Enter your password to continue.",
            "code": CODE,
            "window_seconds": WINDOW_SECONDS,
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def note_failure(user, request) -> None:
    """A wrong password was typed into the confirm step: leave a security event, and on the
    `ALERT_AFTER_FAILURES`th failure within an hour raise one alert and email the owner. Never
    raises (the cache is best-effort; the 403 must still go out)."""
    from origin.models.common.security_event_models import SecurityAction  # noqa: PLC0415
    from origin.services import credential_alert  # noqa: PLC0415
    from origin.services.security_events import record_security_event  # noqa: PLC0415

    try:
        record_security_event(
            SecurityAction.RECENT_AUTH_FAILED,
            request=request,
            actor=user,
            target_type="user",
            target_id=user.pk,
        )
        key = f"recent_auth_fail:{user.pk}"
        count = 1 if cache.add(key, 1, _FAILURE_WINDOW) else cache.incr(key)
        if count != ALERT_AFTER_FAILURES:
            return
        record_security_event(
            SecurityAction.ALERT_RECENT_AUTH_GUESSING,
            request=request,
            actor=user,
            target_type="user",
            target_id=user.pk,
            metadata={"failures": count, "window_seconds": _FAILURE_WINDOW},
        )
        credential_alert.notify_guessing(user)
    except Exception:  # noqa: BLE001
        logger.error("recent-auth failure note failed", exc_info=True)

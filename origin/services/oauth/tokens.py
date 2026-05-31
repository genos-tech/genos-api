"""Lazy access-token refresh.

`get_valid_access_token(connected_account)` is the single entry point
that every feature endpoint (Calendar, GitHub) uses to get a usable
access token. It transparently refreshes if the stored token is about
to expire — no background worker needed.

Refresh policy:
  - GitHub: tokens don't expire (`supports_refresh=False`), so we just
    decrypt and return.
  - Google: refresh when `access_token_expires_at` is within
    `REFRESH_LEEWAY_SECONDS` of now (or already past).
"""

from __future__ import annotations

from datetime import timedelta

import requests
from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto

from .registry import get_provider

# How early before expiry we refresh. 5 minutes is comfortably longer
# than any single request, so a request that *starts* with a valid
# token will finish before it expires mid-call.
REFRESH_LEEWAY_SECONDS = 300


class ReauthRequired(Exception):
    """The stored OAuth credential can no longer be refreshed.

    Raised when the refresh token is gone, or when the provider rejects
    it with the OAuth-standard `invalid_grant` error (RFC 6749) — i.e.
    it's been revoked or has expired. Notably, while a Google OAuth app
    is in "Testing" publishing status its refresh tokens expire after
    ~7 days (and after 6 months of disuse in any status), so a healthy-
    looking, still-scoped account routinely lands here.

    The only fix is for the user to re-run the connect flow, which mints
    a fresh refresh token. Request-context callers should catch this and
    surface an actionable "reconnect" signal instead of letting it 500.
    """

    def __init__(self, account: ConnectedAccount):
        self.account_id = account.id
        self.provider = account.provider
        super().__init__(
            f"ConnectedAccount {account.id} ({account.provider}) needs reauth: "
            "its refresh token is missing, revoked, or expired."
        )


def _is_invalid_grant(exc: requests.HTTPError) -> bool:
    """True only for the OAuth `invalid_grant` failure (HTTP 400 + that
    error code). Other 400s — `invalid_client`, `invalid_request` — are
    config bugs reconnecting won't fix, so they're left to propagate."""
    resp = exc.response
    if resp is None or resp.status_code != 400:
        return False
    try:
        return (resp.json() or {}).get("error") == "invalid_grant"
    except ValueError:
        return False


def get_valid_access_token(account: ConnectedAccount) -> str:
    """Return a plaintext access token that's safe to use right now.

    Side-effect: if a refresh happens, the new ciphertext + expiry are
    saved back to the row.
    """
    provider = get_provider(account.provider)
    if not provider.supports_refresh:
        return crypto.decrypt(account.access_token_encrypted)

    expires_at = account.access_token_expires_at
    now = timezone.now()
    needs_refresh = expires_at is None or expires_at <= now + timedelta(
        seconds=REFRESH_LEEWAY_SECONDS
    )
    if not needs_refresh:
        return crypto.decrypt(account.access_token_encrypted)

    if not account.refresh_token_encrypted:
        raise ReauthRequired(account)

    refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    try:
        fresh = provider.refresh(refresh_token=refresh_token)
    except requests.HTTPError as exc:
        # A dead refresh token (revoked / expired) comes back as a 400
        # `invalid_grant`. That's unrecoverable without user action, so
        # translate it into ReauthRequired; let every other HTTP error
        # (5xx, transient network blips, misconfig) keep propagating.
        if _is_invalid_grant(exc):
            raise ReauthRequired(account) from exc
        raise

    account.access_token_encrypted = crypto.encrypt(fresh.access_token)
    if fresh.refresh_token:
        # Google usually omits this on refresh — only update when one
        # is actually returned, otherwise the existing one stays valid.
        account.refresh_token_encrypted = crypto.encrypt(fresh.refresh_token)
    if fresh.expires_in_seconds is not None:
        account.access_token_expires_at = now + timedelta(seconds=fresh.expires_in_seconds)
    account.save(
        update_fields=[
            "access_token_encrypted",
            "refresh_token_encrypted",
            "access_token_expires_at",
            "ts_updated_at",
        ]
    )
    return fresh.access_token

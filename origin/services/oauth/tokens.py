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

from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto

from .registry import get_provider

# How early before expiry we refresh. 5 minutes is comfortably longer
# than any single request, so a request that *starts* with a valid
# token will finish before it expires mid-call.
REFRESH_LEEWAY_SECONDS = 300


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
        raise RuntimeError(
            f"ConnectedAccount {account.id} has no refresh token but its access "
            "token is expired. The user must reconnect this provider."
        )

    refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    fresh = provider.refresh(refresh_token=refresh_token)

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

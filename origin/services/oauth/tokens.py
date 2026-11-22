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
  - GitHub App (`github_app`, MIP-2086): same expiry rule, but GitHub ROTATES the refresh
    token: using it kills the old refresh token and access token. So the refresh runs under a
    row lock (`select_for_update`) and re-reads the row first: a request that lost the race
    finds the winner's fresh token instead of spending a dead refresh token and losing the
    connection.
"""

from __future__ import annotations

from datetime import timedelta

import requests
from django.db import transaction
from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto

from .base import InvalidGrantError
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

    now = timezone.now()
    if not _needs_refresh(account, now):
        return crypto.decrypt(account.access_token_encrypted)

    # `is True`: a provider stand-in (a test's MagicMock) must not read as rotating.
    rotates = provider.rotates_refresh_token is True
    if rotates and account.access_token_expires_at is None and not account.refresh_token_encrypted:
        # An App registered with "Expire user authorization tokens" OFF: GitHub sends neither an
        # expiry nor a refresh token, and the token never expires. Not a reason to demand a
        # reconnect on every call. (The runbook says to keep expiry on.)
        return crypto.decrypt(account.access_token_encrypted)

    if rotates:
        return _refresh_locked(account, provider)
    return _refresh(account, provider, now)


def _needs_refresh(account: ConnectedAccount, now) -> bool:
    expires_at = account.access_token_expires_at
    return expires_at is None or expires_at <= now + timedelta(seconds=REFRESH_LEEWAY_SECONDS)


_SYNCED_FIELDS = (
    "access_token_encrypted",
    "refresh_token_encrypted",
    "access_token_expires_at",
    "ts_updated_at",
)


def _refresh_locked(account: ConnectedAccount, provider) -> str:
    """Refresh a provider that rotates its refresh token, under a row lock.

    The caller's `account` may be stale: a concurrent request can have rotated the token since it
    was loaded. The locked re-read decides, so at most one request spends a given refresh token.
    The lock is held across the provider call (3 s connect, 10 s read) on purpose: releasing it
    before the new token is saved would let a second request spend the old, now dead, refresh
    token. Do not call this inside a long outer transaction: the lock would last until it commits.

    Known limit: the "needs a refresh?" test in `get_valid_access_token` runs on the caller's
    instance BEFORE this lock, so an instance held for minutes (a cron loop over accounts) can
    return a token another request has since rotated away. It recovers on the next call; load the
    account fresh before use.
    """
    with transaction.atomic():
        locked = ConnectedAccount.objects.select_for_update().filter(pk=account.pk).first()
        if locked is None:
            # Disconnected while this request was in flight.
            raise ReauthRequired(account)
        try:
            now = timezone.now()
            if _needs_refresh(locked, now):
                return _refresh(locked, provider, now)
            return crypto.decrypt(locked.access_token_encrypted)
        finally:
            # Hand the caller's instance what the row holds now, whether this request refreshed
            # or found another's refresh, so a later save of `account` cannot write stale tokens.
            for field in _SYNCED_FIELDS:
                setattr(account, field, getattr(locked, field))


def _refresh(account: ConnectedAccount, provider, now) -> str:
    if not account.refresh_token_encrypted:
        raise ReauthRequired(account)

    refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    try:
        fresh = provider.refresh(refresh_token=refresh_token)
    except InvalidGrantError as exc:
        # A provider that reports a dead refresh token as HTTP 200 + `error` (GitHub).
        raise ReauthRequired(account) from exc
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

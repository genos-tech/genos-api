"""API-key authentication — the first `BaseAuthentication` in this repo.

    Authorization: ApiKey gnos_xxxxxxxx…

## Why not `Bearer`

SimpleJWT already owns `Bearer`. Sharing the keyword would mean guessing
which credential type a token is by its shape, and guessing wrong on a
malformed value produces a confusing 401 from the wrong authenticator.
A distinct keyword makes the two paths unambiguous, and DRF's
`WWW-Authenticate` handling falls out of it for free.

## The trap this file exists to avoid

**SimpleJWT's `is_active` guarantee does not transfer.** That check lives
in simplejwt's `get_user`, not in DRF, so any new authenticator resolves
the user itself and inherits nothing. This repo has already shipped that
exact bug once: OAuth sign-in minted tokens via `RefreshToken.for_user`
without checking `is_active`, so a deactivated user completed the whole
flow and got tokens SimpleJWT then rejected on first use (readiness plan
§5.4).

So this class re-checks **`is_active` AND `is_deleted`** explicitly. The
account-deletion flow sets `is_active=False` and relies on every auth
path honouring it; a key that kept working after deletion would be a
hole in erasure, not just an inconvenience.

Failure returns `AuthenticationFailed` (401) rather than `None`, because
returning `None` means "not my credential" and would let the request
fall through to the next authenticator and end up anonymous — a revoked
key must be a refusal, not a downgrade.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import authentication, exceptions

from origin.models.common.api_key_models import ApiKey, hash_key

KEYWORD = "ApiKey"

# `last_used_at` is a debugging affordance, not an audit record. Writing
# it on every request would double the write cost of the public API to
# power a field nobody reads to the minute.
_LAST_USED_RESOLUTION = timedelta(hours=1)


class ApiKeyAuthentication(authentication.BaseAuthentication):
    keyword = KEYWORD

    def authenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            # Not an API-key request. Returning None lets JWT have it.
            return None
        if len(auth) == 1:
            raise exceptions.AuthenticationFailed("No API key provided.")
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed("API key must not contain spaces.")

        try:
            raw = auth[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Malformed API key.") from None

        key = ApiKey.objects.select_related("user", "team").filter(key_hash=hash_key(raw)).first()
        # One message for "no such key", "revoked" and "expired". Telling
        # a caller which one leaks whether a key ever existed.
        if key is None or not key.is_active:
            raise exceptions.AuthenticationFailed("Invalid or revoked API key.")

        user = key.user
        if user is None or not user.is_active or getattr(user, "is_deleted", False):
            raise exceptions.AuthenticationFailed("Invalid or revoked API key.")

        self._touch(key)
        # `request.auth` carries the key so views can read `.scope` and
        # `.team` — that is how a write endpoint refuses a read-only key
        # without a second lookup.
        return (user, key)

    @staticmethod
    def _touch(key: ApiKey) -> None:
        now = timezone.now()
        if key.last_used_at is not None and now - key.last_used_at < _LAST_USED_RESOLUTION:
            return
        # `update()` rather than `save()`: no signals, no race with a
        # concurrent request writing the same coarse value, and it can
        # never clobber a revocation that landed in between.
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)

    def authenticate_header(self, request):
        return self.keyword

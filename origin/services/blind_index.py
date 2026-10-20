"""Blind indexes: exact-match lookups on columns that are encrypted (MIP-619).

An encrypted column can't be filtered on: every write uses a fresh IV, so
`filter(email=...)` compares ciphertext and never matches. Each searchable
column therefore gets a sibling holding an HMAC-SHA256 of its canonical
form, and lookups and uniqueness go through that sibling instead. The HMAC
key is `BLIND_INDEX_KEYS`, kept apart from `FIELD_ENCRYPTION_KEYS` so a
leaked index key reveals nothing an encryption key protects, and the other
way round.

**Keyring.** Newest key first, like `FIELD_ENCRYPTION_KEYS`. Writes hash
with the newest key; lookups match a value hashed under any key in the
ring, so a rotation is:

1. Put a new key in FRONT of `BLIND_INDEX_KEYS` and redeploy every
   service that holds it.
2. `python manage.py rotate_field_keys` rehashes every row with the new key.
3. Drop the old key and redeploy.

A row hashed with a key no longer in the ring can't be found by its email,
so step 3 only after step 2 reports nothing left to rehash.

**Writes.** `BlindIndexMixin.save` keeps the hash current. `bulk_create`
and `QuerySet.update` skip `save`, so they must never write an indexed
column; nothing does today.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings
from django.core.signals import setting_changed
from django.db.models import Q
from django.dispatch import receiver


class BlindIndexKeyMissing(RuntimeError):
    """No `BLIND_INDEX_KEYS`, so no indexed value can be written or found."""


@lru_cache(maxsize=1)
def _keys() -> tuple[bytes, ...]:
    raw = settings.BLIND_INDEX_KEYS
    if not raw:
        raise BlindIndexKeyMissing(
            "BLIND_INDEX_KEYS is not set. Generate a key with "
            "`python -c 'import secrets, base64; "
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'` "
            "and add it to every genos-api service's environment."
        )
    return tuple(decode_key(k) for k in raw)


@receiver(setting_changed)
def _keys_changed(*, setting, **kwargs):
    # So `override_settings(BLIND_INDEX_KEYS=...)` takes effect.
    if setting == "BLIND_INDEX_KEYS":
        _keys.cache_clear()


def decode_key(key: str) -> bytes:
    """The 32 raw bytes of a url-safe base64 key. `ValueError` otherwise."""
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except (ValueError, TypeError):
        raise ValueError("not url-safe base64") from None
    if len(raw) != 32:
        raise ValueError("not 32 bytes")
    return raw


def require_keys() -> None:
    """Raise `BlindIndexKeyMissing` (or `ValueError`) now, not mid-run.

    For a cron that looks an address up: without it, the first lookup
    fails inside the per-user loop instead of at the start.
    """
    _keys()


def _hmac(key: bytes, canonical: str) -> str:
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class BlindIndex:
    """One indexed column: `source` is hashed into `column`.

    `fold_case` decides what counts as the same value. Addresses fold, as
    every lookup on them already did (`iexact`, or a value stored
    lowercased). `TeamMaster.team_email` doesn't: it's built from the
    case-sensitive team name, so folding would refuse "Acme" next to "acme".
    """

    source: str
    column: str
    fold_case: bool

    def canonical(self, value: str) -> str:
        value = value.strip()
        return value.lower() if self.fold_case else value

    def compute(self, value: str | None) -> str | None:
        """The hash under the newest key; `None` for an empty value."""
        canonical = self.canonical(value) if value else ""
        if not canonical:
            return None
        return _hmac(_keys()[0], canonical)

    def candidates(self, value: str) -> list[str]:
        """The hash under every key in the ring, newest first."""
        canonical = self.canonical(value)
        return [_hmac(key, canonical) for key in _keys()]

    def matches(self, value: str | None, *, prefix: str = "") -> Q:
        """A filter for rows whose `source` equals `value` (canonically).

        `prefix` reaches the column through a relation, e.g. "attendee__".
        An empty value matches nothing, and so does one that isn't a
        string: callers pass `request.data` values, which JSON lets be a
        list.

        A row with no hash isn't found. Only a row the previous release
        wrote during a deploy has none, and `rotate_field_keys` fills it
        in: run it after every deploy.
        """
        if not isinstance(value, str) or not value.strip():
            return Q(pk__in=[])
        return Q(**{f"{prefix}{self.column}__in": self.candidates(value)})


USER_EMAIL = BlindIndex(source="email", column="email_hash", fold_case=True)
TEAM_EMAIL = BlindIndex(source="team_email", column="team_email_hash", fold_case=False)
INVITE_EMAIL = BlindIndex(source="invited_email", column="invited_email_hash", fold_case=True)
SUPPRESSED_ADDRESS = BlindIndex(source="address", column="address_hash", fold_case=True)


ALL = (USER_EMAIL, TEAM_EMAIL, INVITE_EMAIL, SUPPRESSED_ADDRESS)


class BlindIndexMixin:
    """Keeps each of a model's `blind_indexes` current on `save()`.

    A `save(update_fields=[...])` that names an indexed column saves its
    hash too; one that doesn't leaves the hash alone, so a cron touching
    some other column needs no key.
    """

    blind_indexes: tuple[BlindIndex, ...] = ()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        touched = []
        for index in self.blind_indexes:
            if update_fields is None or index.source in update_fields:
                setattr(self, index.column, index.compute(getattr(self, index.source)))
                touched.append(index.column)
        if update_fields is not None and touched:
            kwargs["update_fields"] = {*update_fields, *touched}
        return super().save(*args, **kwargs)

"""Symmetric encryption helpers for sensitive data at rest.

Used for OAuth access / refresh tokens (`ConnectedAccount`), outbound
webhook signing secrets (`WebhookEndpoint`) and Web Push subscription
keys (`PushSubscription`), so anyone who only has DB-row access (a dump,
a backup, a replica) can't read them. Fernet (AES-128-CBC +
HMAC-SHA256) from the `cryptography` library.

**Keyring (MIP-618).** `FIELD_ENCRYPTION_KEYS` is a list, newest first.
New values are always encrypted with the first key; decryption tries each
key in turn (`MultiFernet`). Rotating a key without downtime is therefore:

1. Put a new key in FRONT of the list and redeploy every service that
   holds it. Old ciphertext still decrypts with the second key.
2. `python manage.py rotate_field_keys` re-encrypts every stored value
   with the new key.
3. Drop the old key from the list and redeploy.

Each Fernet token carries no key id; `MultiFernet` finds the right key by
trying them, and a token that none of them opens raises `InvalidToken`.

The Fernet instance is built lazily on first use, so a dev setup that
never encrypts anything can leave the key empty. The web server refuses
to boot without a valid keyring outside DEBUG (`validate_web_config`).
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.signals import setting_changed
from django.db import models
from django.dispatch import receiver


class KeyringMissing(RuntimeError):
    """No key is configured, so nothing can be encrypted or decrypted."""


@lru_cache(maxsize=1)
def _get_fernet() -> tuple[MultiFernet, Fernet]:
    """The whole ring, and its newest key on its own."""
    keys = settings.FIELD_ENCRYPTION_KEYS
    if not keys:
        raise KeyringMissing(
            "FIELD_ENCRYPTION_KEYS is not set. Generate a key with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to the "
            "backend's environment before using OAuth, webhooks or Web Push."
        )
    fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
    return MultiFernet(fernets), fernets[0]


@receiver(setting_changed)
def _keyring_changed(*, setting, **kwargs):
    # So `override_settings(FIELD_ENCRYPTION_KEYS=...)` takes effect.
    if setting == "FIELD_ENCRYPTION_KEYS":
        _get_fernet.cache_clear()


def require_keyring() -> None:
    """Raise `KeyringMissing` (or `ValueError` for a malformed key) now.

    For a cron that decrypts: without it, a missing key surfaces only
    inside a push or delivery, where the error is swallowed and logged,
    so the run stays green while nothing is sent.
    """
    _get_fernet()


def encrypt(plaintext: str) -> str:
    return _get_fernet()[0].encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet()[0].decrypt(ciphertext.encode()).decode()


def rotate(ciphertext: str) -> str:
    """The same plaintext, re-encrypted with the newest key."""
    return _get_fernet()[0].rotate(ciphertext.encode()).decode()


def needs_rotation(ciphertext: str) -> bool:
    """Whether `ciphertext` was encrypted with a key other than the newest.

    Raises `InvalidToken` when no key in the ring opens it.
    """
    ring, newest = _get_fernet()
    try:
        newest.decrypt(ciphertext.encode())
    except InvalidToken:
        ring.decrypt(ciphertext.encode())  # raises if no key at all opens it
        return True
    return False


# Every Fernet token starts with version byte 0x80 and an 8-byte
# timestamp, which base64url-encodes to this prefix, and is at least 100
# characters long (57 bytes of overhead for an empty plaintext). Used
# only to tell a value written before a field was encrypted from one
# written after: browser push keys are 22 (auth) and 87 (p256dh)
# characters, so none of them can pass both tests.
_TOKEN_PREFIX = "gAAAAA"
_TOKEN_MIN_LENGTH = 100


def looks_encrypted(value: str) -> bool:
    return value.startswith(_TOKEN_PREFIX) and len(value) >= _TOKEN_MIN_LENGTH


def refuse_ciphertext_lookalike(value) -> None:
    """Validator: refuse a value that looks like a Fernet token.

    Once a column is strictly encrypted, a stored value that looks like a
    token but that no key opens raises on every read of its row. A user
    who planted one in their profile would break every roster they're in.
    """
    if isinstance(value, str) and looks_encrypted(value):
        raise ValidationError(
            "This value can't be saved: it looks like encrypted data.",
            code="ciphertext_lookalike",
        )


class _DecryptOnRead:
    """Reads ciphertext back as plaintext, and writes plaintext (MIP-619).

    For a column in the release BEFORE the one that encrypts it. Railway
    runs the encrypting release's migration while this release still
    serves, so this release has to read the ciphertext that migration
    writes. What this release itself writes stays plaintext, which the
    encrypting release reads as-is and `rotate_field_keys` then encrypts.

    Nothing writes ciphertext here except that later release, with a key
    this release also holds. So a value that only looks like a token, and
    that no key opens, is plaintext: it's returned as stored, where
    `EncryptedTextField` would raise. The serializers refuse such values
    (the validator below), so a user can't plant one for the encrypting
    release to trip over.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Appended, not a class attribute: that would shadow the base
        # field's own (EmailField's address check).
        self.validators.append(refuse_ciphertext_lookalike)

    def from_db_value(self, value, expression, connection):
        if value is None or not looks_encrypted(value):
            return value
        try:
            return decrypt(value)
        except InvalidToken:
            # `KeyringMissing` still raises: ciphertext handed back as an
            # address would be mailed to.
            return value


class DecryptingEmailField(_DecryptOnRead, models.EmailField):
    pass


class DecryptingCharField(_DecryptOnRead, models.CharField):
    pass


class DecryptingTextField(_DecryptOnRead, models.TextField):
    pass


class EncryptedTextField(models.TextField):
    """A text column stored as Fernet ciphertext, read back as plaintext.

    Encrypts on every write path that goes through the ORM (`save`,
    `create`, `update_or_create`, `bulk_create`, `QuerySet.update`), so a
    caller can't forget to. Rows are not searchable or comparable by
    value: `filter(field=...)` compares ciphertext and never matches.

    A stored value that isn't ciphertext is returned as-is. That's a row
    written before the column was encrypted, or by a container still
    running the previous release during a deploy; the data migration
    and `rotate_field_keys` encrypt them. Ciphertext that no key opens
    raises `InvalidToken` instead of reaching the caller as garbage.
    """

    def from_db_value(self, value, expression, connection):
        if value is None or not looks_encrypted(value):
            return value
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(value)

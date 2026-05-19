"""Symmetric encryption helpers for sensitive data at rest.

Currently used by `ConnectedAccount` to keep OAuth access / refresh
tokens unreadable to anyone who only has DB-row access. Uses Fernet
(AES-128-CBC + HMAC-SHA256, with a key-versioned envelope) from the
`cryptography` library.

The Fernet instance is constructed lazily on first use so an empty
`OAUTH_TOKEN_ENCRYPTION_KEY` doesn't break Django startup in dev. The
key is required only when something actually tries to encrypt or
decrypt — i.e. when a user kicks off an OAuth flow. Production must
set it; dev that never exercises OAuth can leave it empty.
"""

from functools import lru_cache

from cryptography.fernet import Fernet
from django.conf import settings


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = settings.OAUTH_TOKEN_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "OAUTH_TOKEN_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to the "
            "backend's environment before using OAuth."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()

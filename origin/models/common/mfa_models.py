"""Two-factor sign-in state (MIP-2239, AUTH_SECURITY_PLAN §4).

`UserMFA` is one row per person who started setting up an authenticator app. It is
*enabled* only once `confirmed_at` is set, which happens when they type a first valid code:
a row without it is a half-finished setup and changes nothing about how they sign in.

Nothing here is readable from a database dump: the secret is Fernet-encrypted (the same
keyring as OAuth tokens), and the recovery codes are stored as SHA-256 hashes. Recovery
codes are 60 random bits and a code is single-use, so a plain hash is enough (the same
argument as `ApiKey`): there is nothing a person chose to brute-force.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from django.db import models

from origin.models.common.user_models import CustomUser

# 12 characters from a 31-letter alphabet with no 0/O/1/I/L: about 59 bits, typed from a printout.
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_LENGTH = 12
RECOVERY_CODE_COUNT = 10


def generate_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, RECOVERY_CODE_LENGTH, 4))


def normalize_recovery_code(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def hash_recovery_code(value: str) -> str:
    return hashlib.sha256(normalize_recovery_code(value).encode()).hexdigest()


class UserMFA(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="mfa")
    # Fernet ciphertext of the base32 TOTP secret. Never returned after setup.
    secret_encrypted = models.TextField()
    # Null while the setup is unfinished. Set = two-factor sign-in is on.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # The last accepted time step: a code for this step or an earlier one is a replay.
    last_used_step = models.BigIntegerField(default=0)
    # Consecutive wrong second factors, and the lock they earned (see `services/mfa.py`).
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_mfa"

    @property
    def is_enabled(self) -> bool:
        return self.confirmed_at is not None

    def __str__(self) -> str:
        return f"UserMFA({self.user_id}, enabled={self.is_enabled})"


class MFARecoveryCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="mfa_recovery_codes"
    )
    code_hash = models.CharField(max_length=64)
    # Set once, atomically, by the update that spends the code.
    used_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_mfa_recovery_code"
        indexes = [models.Index(fields=["user", "code_hash"], name="mfa_recovery_user_hash_idx")]

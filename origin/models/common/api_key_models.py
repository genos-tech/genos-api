"""API keys for the public API (readiness plan §4.4).

Two principals, one table, distinguished by whether `team` is set:

  team IS NULL   a PERSONAL ACCESS TOKEN. Acts as `user`, inheriting
                 their ACL exactly. No new authorization surface — if
                 the person can't see it, neither can their token.
  team IS SET    a TEAM-SCOPED key, created by an owner/editor for a bot
                 or integration. Still acts as `user` for ACL purposes
                 (there is no non-human principal in this codebase, and
                 inventing one would mean teaching every ACL check,
                 audit trail and attribution surface about it), but it
                 is bound to one team and survives its creator changing
                 roles — the thing a personal token cannot do.

## The key itself is never stored

Only a SHA-256 hash and a display prefix. The plaintext is returned once,
at creation, and cannot be recovered — same contract as `TeamInvite`'s
token. A DB dump therefore yields no usable credential.

The hash is deliberately plain SHA-256 rather than a password hash. API
keys are 32 bytes of `secrets.token_urlsafe` entropy, not user-chosen
secrets, so there is nothing to brute-force and a deliberately slow KDF
would just add latency to every authenticated request. That reasoning
does NOT transfer to anything a human picks.

## Revocation must be immediate

`revoked_at` is checked on every request rather than relying on a cache,
because the whole point of revocation is that it takes effect the moment
someone realises a key has leaked.
"""

import hashlib
import secrets
import uuid

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

# Recognisable in a leaked log or a pasted config, and greppable in a
# secret scanner. GitHub's own convention: a fixed prefix beats an
# opaque blob nobody can attribute.
KEY_PREFIX = "gnos_"
# Bytes of entropy. 32 → ~43 urlsafe chars.
KEY_BYTES = 32
# How much of the key is stored in the clear so a person can tell two
# keys apart in a list. Short enough to be useless on its own.
DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 6

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_CHOICES = [(SCOPE_READ, "read"), (SCOPE_WRITE, "write")]


def generate_key() -> str:
    """A fresh plaintext key. Shown once, never stored."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The principal the key acts as. CASCADE because a key with no user
    # has no ACL and must not outlive them; account deletion deactivates
    # the user anyway (§5.4), and the auth class re-checks that.
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="api_keys")
    # NULL → personal access token. Set → team-scoped key.
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="api_keys",
        to_field="team_id",
    )
    name = models.CharField(max_length=100)
    # Indexed because it is the lookup column on every authenticated
    # request — the hash, not the key.
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=32)
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default=SCOPE_READ)
    # Coarse: updated at most once an hour (see the auth class). A write
    # per request would double the cost of every API call to power a
    # field nobody reads to the minute.
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "revoked_at"]),
            models.Index(fields=["team", "revoked_at"]),
        ]
        ordering = ["-ts_created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and not self.is_expired

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "ts_updated_at"])

import uuid

from django.db import models
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

INVITE_STATUS_CHOICES = [
    ("pending", "pending"),
    ("accepted", "accepted"),
    ("revoked", "revoked"),
]


class TeamInvite(models.Model):
    """An email invitation to join a team.

    Mirrors the password-reset / email-verification token model: the URL
    carries a raw `secrets.token_urlsafe(32)` token; we only ever store
    its SHA-256 hash here, so a DB dump can't reconstruct live invite
    links. The invite is single-use (status flips to `accepted`) and
    locked to `invited_email` — `accept_invite` rejects a user whose
    email doesn't match, so a forwarded link can't pull a stranger in.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="invites",
        to_field="team_id",
    )
    # Always stored lowercased so the unique-ish lookup and the
    # accept-time match are case-insensitive without per-query iexact.
    invited_email = models.EmailField()
    invited_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invites",
        to_field="id",
    )
    token_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 hex
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16, choices=INVITE_STATUS_CHOICES, default="pending", db_index=True
    )
    # Who actually consumed the invite (may differ from invited_email's
    # account only in theory — match is enforced — but recorded for audit).
    accepted_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        to_field="id",
    )
    ts_accepted_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["team", "invited_email"])]

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
    """An email invitation to join a team, or to one project as a guest.

    Mirrors the password-reset / email-verification token model: the URL
    carries a raw `secrets.token_urlsafe(32)` token; we only ever store
    its SHA-256 hash here, so a DB dump can't reconstruct live invite
    links. The invite is single-use (status flips to `accepted`) and
    locked to `invited_email` — `accept_invite` rejects a user whose
    email doesn't match, so a forwarded link can't pull a stranger in.

    ## Guest invites reuse this table on purpose

    A guest is an EXTERNAL person, so they have no inbox to receive a
    request in and no workspace to browse — an emailed token is the only
    entry point that makes sense. That also means guests need no new
    delivery mechanism, no new `item_type`, and no frontend inbox work:
    the accept-invite page already exists.

    What differs is what acceptance WRITES. A normal invite creates a
    `TeamMembers` row; a guest invite creates a `ProjectMembers` row and
    deliberately no team row at all (see `services/member_roles`). The
    two extra columns below carry exactly that difference and nothing
    else.
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
    # What acceptance grants. `viewer` / `editor` mean a TeamMembers row;
    # `guest` means a ProjectMembers row in `project` and NO team row.
    # Defaulted rather than nullable so every historical invite reads as
    # the ordinary team invite it was.
    member_role = models.CharField(max_length=16, default="viewer")
    # Set only for guest invites — the single project the guest is scoped
    # to. SET_NULL rather than CASCADE so deleting a project doesn't
    # silently vanish the audit trail of who was invited to it; a guest
    # invite whose project has gone is refused at accept time instead.
    project = models.ForeignKey(
        "origin.ProjectMaster",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guest_invites",
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

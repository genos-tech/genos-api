"""Team-membership helpers shared across the join / invite flows.

`add_team_member` centralises the un-delete-or-create logic that the
direct-join and inbox-approval views implement inline; `accept_invite`
validates and consumes a `TeamInvite` and is the single source of truth
for both the accept endpoint and the invite-signup path.
"""

from django.db import transaction
from django.utils import timezone

from origin.models.common.team_models import TeamMembers


class InviteAcceptError(Exception):
    """Raised when a TeamInvite cannot be consumed.

    `code` is a stable string the API surfaces to the client:
    `invalid` | `expired` | `email_mismatch` | `team_unavailable`.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def add_team_member(team_id, attendee_id) -> None:
    """Un-delete-or-create a TeamMembers row.

    Mirrors the re-join path in `TeamMembersView.post`: a previously
    soft-deleted membership is reactivated in place so the
    (team, attendee) unique constraint isn't violated.
    """
    existing = TeamMembers.objects.filter(team_id=team_id, attendee_id=attendee_id).first()
    if existing is not None:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.save(update_fields=["is_deleted", "ts_updated_at"])
        return
    TeamMembers.objects.create(team_id=team_id, attendee_id=attendee_id)


def accept_invite(invite, user):
    """Validate and consume `invite` on behalf of `user`.

    Raises `InviteAcceptError(code)` on any failure; returns the joined
    team on success. Membership add + invite status flip happen in one
    transaction so a crash can't leave a half-consumed invite.
    """
    if invite.status != "pending":
        # Already accepted or revoked — don't leak which.
        raise InviteAcceptError("invalid")
    if invite.expires_at <= timezone.now():
        raise InviteAcceptError("expired")
    if user.email.lower() != invite.invited_email.lower():
        raise InviteAcceptError("email_mismatch")

    team = invite.team
    if team is None or team.is_deleted:
        raise InviteAcceptError("team_unavailable")

    with transaction.atomic():
        add_team_member(team.team_id, user.id)
        invite.status = "accepted"
        invite.accepted_by = user
        invite.ts_accepted_at = timezone.now()
        invite.save(update_fields=["status", "accepted_by", "ts_accepted_at", "ts_updated_at"])
    return team

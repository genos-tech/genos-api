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


def drop_in_team_access(team_id, attendee_id) -> None:
    """Give up the access the departing membership underwrote INSIDE the team.

    `TeamMembers` is not what most reads check. A project asks
    `ProjectMembers`, a channel asks `ChannelMember`, a team folder asks
    `NoteFolderPermission` — each keyed on the user and the object, none
    of them on the membership row. So a departure that stopped at the
    membership row left every one of those rows granting exactly what it
    granted before, and the project rows did worse than keep access:
    `GetMyTeamsView` synthesizes a GUEST SHELL for any team the caller
    holds a `ProjectMembers` row in, so the team someone just left
    reappeared in their switcher with its projects still open. Same rule
    as `drop_team_member_participation`, applied to the near side.

    Rows a cross-team grant wrote are left alone where the table records
    that provenance (`NoteFolderPermission.via_group_type`): those are
    underwritten by ANOTHER team's roster and are that grant's business,
    not this departure's. Channel and project rows carry no provenance
    column — one row serves native and granted access alike — so they
    go, and the guest team's managers can re-admit through
    `add_external_participants`, which is the path that wrote them.
    Erring toward revoking is deliberate: a row wrongly kept is silent
    access after leaving, a row wrongly dropped is a re-invite.
    """
    from origin.models.chat.unified_models import ChannelMember
    from origin.models.note.common_note_models import NoteFolderPermission
    from origin.models.project.prj_models import ProjectMembers

    # Scoped by the PROJECT's team rather than the row's denormalized
    # `team_id`, which is nullable. A hard delete because the table has no
    # `is_deleted` column, and the `post_delete` receiver on it soft-deletes
    # the matching PM `ChannelMember` row (`signals/pm_channel_signals.py`).
    ProjectMembers.objects.filter(project__team_id=team_id, attendee_id=attendee_id).delete()

    # Every channel of the team at once — GM, PM, DM, note chats. Soft,
    # because that is this table's convention, and the re-entry paths all
    # un-delete in place (`ChannelMembersView.post`, the DM reopen in
    # `channel_views`, the PM backfill receiver).
    ChannelMember.objects.filter(
        channel__team_id=team_id, user_id=attendee_id, is_deleted=False
    ).update(is_deleted=True)

    # Keyed on the `team` column rather than the folder's, because that is
    # the column the read path resolves through (`load_my_folder_grants`
    # filters `team=`), so it is exactly the set of rows that can grant
    # anything in this team.
    NoteFolderPermission.objects.filter(team_id=team_id, user_id=attendee_id).exclude(
        via_group_type="external_grant"
    ).delete()


def remove_team_member(team_id, attendee_id) -> int:
    """Soft-delete a membership row and strip the access it underwrote.

    Soft-delete preserves the rejoin path: `TeamMembersView.post`
    un-deletes rather than inserting a duplicate, which the unique
    constraint would refuse.

    The cascade is the part that is easy to miss, and it runs in two
    directions. Outward, under cross-team sharing a team's roster is the
    authority on who may reach ANOTHER team's shared objects — the guest
    team's managers admit their own people, and nothing re-checks that at
    read time — so stopping at this row would leave someone who quit team
    B holding access to team A's project indefinitely. Inward, the team's
    own projects, channels and note folders each keep their own
    membership row, and those outlive the departure too; see
    `drop_in_team_access`. Returns the number of cross-team participation
    rows withdrawn.

    Deliberately a function call rather than a `post_save` signal on
    `TeamMembers`: a security cascade should be readable at the point the
    departure is written, not discovered later by grepping for receivers.
    """
    from origin.services.external_grants import drop_team_member_participation

    with transaction.atomic():
        member = TeamMembers.objects.filter(
            team_id=team_id, attendee_id=attendee_id, is_deleted=False
        ).first()
        if member is not None:
            member.is_deleted = True
            member.save(update_fields=["is_deleted", "ts_updated_at"])
        drop_in_team_access(team_id, attendee_id)
        return drop_team_member_participation(team_id, attendee_id)


def add_project_guest(team_id, project_id, attendee_id) -> None:
    """Un-delete-or-create the `ProjectMembers` row for a guest.

    Deliberately does NOT touch `TeamMembers`. That absence is the guest
    model — see `services/member_roles` — and writing a team row here
    would silently hand the guest the whole workspace.

    `ProjectMembers` has no `is_deleted` column (removal is a hard
    delete), so `update_or_create` is enough; the role is refreshed on a
    re-invite in case someone was previously a full member of the
    project and is being re-scoped down to a guest.
    """
    from origin.models.project.prj_models import ProjectMembers
    from origin.services.member_roles import GUEST

    ProjectMembers.objects.update_or_create(
        project_id=project_id,
        attendee_id=attendee_id,
        defaults={"team_id": team_id, "member_role": GUEST},
    )


def accept_invite(invite, user):
    """Validate and consume `invite` on behalf of `user`.

    Raises `InviteAcceptError(code)` on any failure; returns the joined
    team on success. Membership add + invite status flip happen in one
    transaction so a crash can't leave a half-consumed invite.

    A GUEST invite takes the same validation path and writes a different
    row: `ProjectMembers` only, no `TeamMembers`. Everything before the
    branch — single-use, expiry, email lock — is shared on purpose, so a
    guest link is exactly as hard to forward or replay as a team link.
    """
    from origin.services.member_roles import GUEST

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

    is_guest_invite = invite.member_role == GUEST
    if is_guest_invite:
        # The project FK is SET_NULL, so a deleted project leaves the
        # invite pointing at nothing. Refuse rather than fall through to
        # the team-join branch, which would grant far more than intended.
        project = invite.project
        if project is None or project.is_deleted:
            raise InviteAcceptError("project_unavailable")

    with transaction.atomic():
        if is_guest_invite:
            add_project_guest(team.team_id, invite.project_id, user.id)
        else:
            add_team_member(team.team_id, user.id)
        invite.status = "accepted"
        invite.accepted_by = user
        invite.ts_accepted_at = timezone.now()
        invite.save(update_fields=["status", "accepted_by", "ts_accepted_at", "ts_updated_at"])
    return team

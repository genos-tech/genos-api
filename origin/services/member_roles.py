"""Membership roles shared by Team, Project and GM.

Three roles, one vocabulary:

  owner   Exactly one per entity. Only they may do destructive things:
          delete the entity, and hand ownership to someone else.
  editor  Everything the owner can do EXCEPT those two. Invite/add
          members, rename, change the avatar, and set other members'
          roles. Exists because a single owner was a hard bottleneck on
          day-to-day admin.
  viewer  Read-only. The default, and what every pre-existing non-owner
          member becomes.

## Do not confuse this with `CustomUser.role`

`CustomUser.role` is the user's self-declared JOB TITLE ("Engineer",
"Designer") — see the `UserProfileRole` picker on the frontend. It is
already serialized as `"role"` in every member payload. This permission
axis is therefore serialized as **`memberRole`** everywhere. Keeping the
two apart is why the column is `member_role`, not `role`.

## Owner lives in the FK, never in the column

`TeamMaster.owner` / `ProjectMaster.owner` / `Channel.owner_id` remain
the single source of truth for ownership. The `member_role` column only
ever encodes the editor/viewer axis — the owner's own row typically
still reads `"viewer"` (the column default), which is harmless because
nothing reads the column without going through `resolve_*_role` below.

That split is deliberate: ownership transfer stays a one-field update
with no role-column bookkeeping to keep in sync, and there is no way to
end up with zero (or two) owners.

**Every permission check must go through a resolver.** A check written
directly against the column would deny the actual owner, whose stored
value is `viewer`.
"""

from __future__ import annotations

OWNER = "owner"
EDITOR = "editor"
VIEWER = "viewer"

# Roles a member can be *assigned*. `owner` is deliberately absent:
# minting an owner is an ownership transfer, which is owner-only and
# goes through its own endpoint.
ASSIGNABLE_ROLES = (EDITOR, VIEWER)

# Roles allowed to manage — invite/add members, rename, change the
# avatar, and set other members' roles.
MANAGER_ROLES = (OWNER, EDITOR)


def can_manage(role: str | None) -> bool:
    """May this role perform non-destructive management actions?"""
    return role in MANAGER_ROLES


def is_assignable(role: str | None) -> bool:
    """Is this a role a manager may assign to another member?"""
    return role in ASSIGNABLE_ROLES


def resolve_team_role(team, user_id, member_row_role: str | None = None) -> str:
    """Effective role of `user_id` in `team`.

    Pass `member_row_role` when the caller already has the row in hand
    to avoid a second query; otherwise it's looked up.
    """
    if team is not None and team.owner_id is not None and str(team.owner_id) == str(user_id):
        return OWNER
    if member_row_role is not None:
        return member_row_role
    from origin.models.common.team_models import TeamMembers

    row = TeamMembers.objects.filter(
        team_id=team.team_id, attendee_id=user_id, is_deleted=False
    ).first()
    return row.member_role if row else VIEWER


def resolve_project_role(project, user_id, member_row_role: str | None = None) -> str:
    """Effective role of `user_id` in `project`. Mirrors `resolve_team_role`."""
    if (
        project is not None
        and project.owner_id is not None
        and str(project.owner_id) == str(user_id)
    ):
        return OWNER
    if member_row_role is not None:
        return member_row_role
    from origin.models.project.prj_models import ProjectMembers

    row = ProjectMembers.objects.filter(project_id=project.project_id, attendee_id=user_id).first()
    return row.member_role if row else VIEWER


# ── GM / channel mapping ───────────────────────────────────────────
#
# `ChannelMember.role` predates this feature with its own vocabulary
# (`owner | admin | member | system`) and is load-bearing for messaging,
# so it is NOT migrated. Instead the two vocabularies are mapped at the
# API boundary: `admin` (a value that existed but was never written)
# becomes `editor`, and `member` — what every current non-owner already
# is — becomes `viewer`. That means zero migration and zero changes to
# the chat write paths.
#
# `system` deliberately has no mapping: system users aren't people and
# must never appear as assignable.
CHANNEL_ROLE_EDITOR = "admin"
CHANNEL_ROLE_VIEWER = "member"

_CHANNEL_TO_MEMBER_ROLE = {
    CHANNEL_ROLE_EDITOR: EDITOR,
    CHANNEL_ROLE_VIEWER: VIEWER,
}
_MEMBER_TO_CHANNEL_ROLE = {
    EDITOR: CHANNEL_ROLE_EDITOR,
    VIEWER: CHANNEL_ROLE_VIEWER,
}


def channel_role_to_member_role(channel_role: str | None) -> str:
    """Map a stored `ChannelMember.role` onto the shared vocabulary.

    Note `"owner"` maps to VIEWER here, not OWNER. Unlike Team and
    Project, the channel table DOES store an owner value — so there are
    two sources of truth and they can disagree (an ownership transfer
    that updated `Channel.owner_id` but left a stale row behind). The FK
    is authoritative, so a row claiming ownership without backing from
    `Channel.owner_id` is treated as an ordinary member and the
    disagreement self-heals. Callers that need the owner overlay use
    `resolve_gm_role` (server) or `resolveDisplayRole` (client).
    """
    return _CHANNEL_TO_MEMBER_ROLE.get(channel_role, VIEWER)


def member_role_to_channel_role(member_role: str) -> str:
    """Inverse of the above, for writes. Only assignable roles map."""
    return _MEMBER_TO_CHANNEL_ROLE[member_role]


def resolve_gm_role(channel, user_id, member_row_role: str | None = None) -> str:
    """Effective role of `user_id` in `channel`. FK wins over the column."""
    if (
        channel is not None
        and channel.owner_id is not None
        and str(channel.owner_id) == str(user_id)
    ):
        return OWNER
    if member_row_role is not None:
        return channel_role_to_member_role(member_row_role)
    from origin.models.chat.unified_models import ChannelMember

    row = ChannelMember.objects.filter(channel=channel, user_id=user_id, is_deleted=False).first()
    return channel_role_to_member_role(row.role) if row else VIEWER

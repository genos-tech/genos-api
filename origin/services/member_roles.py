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

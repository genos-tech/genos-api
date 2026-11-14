"""A group or project chat whose owner is a deleted account gets a live owner (MIP-1550).

`Channel.owner` is a nullable FK with `SET_NULL`, but account deletion is a soft delete (the row
is anonymised, never removed), so the FK never cleared: a group kept pointing at "Deleted user" as
its owner. Every owner-only action (transfer, remove an editor, delete someone's message, leave
refusal, the join-request inbox item) then addressed or waited on a person who cannot act.

The rule, per kind. A chat is NEVER deleted.

* **GM (group)**: the owner becomes the LONGEST-STANDING remaining member: the live member with
  the earliest `ts_joined_at` (id breaks a tie) who is also a live member of the chat's own team.
  The team condition keeps an external chat's ownership on the host side: a guest-team member is
  admitted by their own team's managers and must not be handed the host's chat. Their row's role
  column is written `owner` (what the transfer endpoint writes), the old owner's row `member`.
  No such member: the chat is left OWNERLESS (`owner=None`): nobody on the host side is left to
  act. The owner checks then fall back to "any member" (see `acts_as_owner` below): a live member
  of the chat who is also on the chat's own team acts as its owner, so a group never strands.
* **PM (project chat)**: the chat MIRRORS the project (`pm_channel_signals` writes
  `owner_id` from `ProjectMaster.owner` on every project save), so the chat's owner can only be
  changed by changing the project's. The project goes to the team owner, and only when the team
  owner is ALREADY on the project (`ProjectMembers`): they become its owner, nobody gains a seat.
  If they are not, the project and its chat are left as they are and counted.
* **DM and MDM**: not touched (they have no owner role).

Only chats whose owner is the one deleted account are selected, and only that owner's own role
column is rewritten. A live account is never passed in: `prune` selects by `owner__is_deleted`,
and `reassign_owned_channels` is called by `delete_account` with the id that is being deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Exists, OuterRef

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.security_events import record_security_event

log = logging.getLogger("origin.mcp")


@dataclass(frozen=True)
class OwnerCounts:
    """What happened (or would) to the chats a deleted account owned. Counts only."""

    accounts: int = 0
    chats: int = 0  # group / project chats looked at
    transferred: int = 0  # a group handed to a remaining member
    projects_transferred: int = 0  # a project (and so its chat) handed to the team owner
    ownerless: int = 0  # a group with nobody to hand it to: owner cleared
    left: int = 0  # a project chat whose team owner is not on the project: unchanged


def _successor_id(channel: Channel, *, excluding):
    """The longest-standing live member of the chat, on the chat's own team, or None."""
    on_team = TeamMembers.objects.filter(
        team_id=channel.team_id, attendee_id=OuterRef("user_id"), is_deleted=False
    )
    return (
        ChannelMember.objects.filter(channel=channel, is_deleted=False, user__is_deleted=False)
        .exclude(user_id=excluding)
        .filter(Exists(on_team))
        .order_by("ts_joined_at", "id")
        .values_list("user_id", flat=True)
        .first()
    )


def _reassign_group(channel: Channel, dead_id) -> str:
    """Hand a group to its longest-standing member. Returns `transferred` or `ownerless`."""
    new_id = _successor_id(channel, excluding=dead_id)
    with transaction.atomic():
        channel.owner_id = new_id
        channel.save(update_fields=["owner", "ts_updated_at"])
        # The old owner's row stops saying `owner` (the FK is the authority, but a stale row is
        # what the role mapping documents as a disagreement to avoid).
        ChannelMember.objects.filter(channel=channel, user_id=dead_id, role="owner").update(
            role="member"
        )
        if new_id is not None:
            ChannelMember.objects.filter(channel=channel, user_id=new_id, is_deleted=False).update(
                role="owner"
            )
    return "transferred" if new_id is not None else "ownerless"


def _reassign_project_chat(channel: Channel, dead_id) -> str:
    """Hand the project (whose chat mirrors it) to the team owner already on it.

    Returns `projects_transferred` or `left`."""
    project = ProjectMaster.objects.filter(project_id=channel.project_id).first()
    team = TeamMaster.objects.filter(team_id=channel.team_id).first()
    if project is None or team is None or str(project.owner_id) != str(dead_id):
        return "left"
    new_id = team.owner_id
    live_owner = (
        new_id is not None
        and str(new_id) != str(dead_id)
        and TeamMembers.objects.filter(team_id=team.team_id, attendee_id=new_id, is_deleted=False)
        .filter(attendee__is_deleted=False)
        .exists()
        and ProjectMembers.objects.filter(
            project_id=project.project_id, attendee_id=new_id
        ).exists()
    )
    if not live_owner:
        return "left"
    with transaction.atomic():
        project.owner_id = new_id
        # The post_save receiver writes the new owner onto the project chat.
        project.save(update_fields=["owner", "ts_updated_at"])
        # The same trail a hand-over through the project page leaves (`prj_views`).
        record_security_event(
            SecurityAction.OWNER_TRANSFERRED,
            actor=None,
            actor_label="system",
            team_id=project.team_id,
            target_type="project",
            target_id=project.project_id,
            target_label=project.project_name,
            metadata={
                "from_user_id": str(dead_id),
                "to_user_id": str(new_id),
                "via": "account_deleted",
            },
        )
    return "projects_transferred"


def _reassign(channel: Channel, dead_id) -> str:
    if channel.kind == ChannelKind.PM:
        return _reassign_project_chat(channel, dead_id)
    return _reassign_group(channel, dead_id)


def _owned_by(user_id):
    return Channel.objects.filter(
        owner_id=user_id, is_deleted=False, kind__in=[ChannelKind.GM, ChannelKind.PM]
    )


def _tally(results: list[str], accounts: int) -> OwnerCounts:
    return OwnerCounts(
        accounts=accounts,
        chats=len(results),
        transferred=results.count("transferred"),
        projects_transferred=results.count("projects_transferred"),
        ownerless=results.count("ownerless"),
        left=results.count("left"),
    )


def reassign_owned_channels(user_id) -> OwnerCounts:
    """Give every group / project chat `user_id` owns a live owner. Idempotent."""
    results = [_reassign(channel, user_id) for channel in list(_owned_by(user_id))]
    return _tally(results, 1 if results else 0)


def _dangling():
    return Channel.objects.filter(
        owner__is_deleted=True, is_deleted=False, kind__in=[ChannelKind.GM, ChannelKind.PM]
    )


def pending_dangling_owners() -> OwnerCounts:
    """How many chats are owned by ALREADY-deleted accounts (what `prune` would touch).

    The `transferred` / `ownerless` / `left` split is only known after the work is done, so a
    dry run reports the chats and the accounts that own them."""
    chats = _dangling()
    return OwnerCounts(accounts=chats.values("owner_id").distinct().count(), chats=chats.count())


def prune(*, apply: bool) -> OwnerCounts:
    """Dry run (default) or apply `reassign_owned_channels` to every already-deleted owner."""
    pending = pending_dangling_owners()
    if not apply or not pending.chats:
        return pending
    dead_ids = list(_dangling().values_list("owner_id", flat=True).distinct())
    results: list[str] = []
    for dead_id in dead_ids:
        results.extend(_reassign(channel, dead_id) for channel in list(_owned_by(dead_id)))
    return _tally(results, len(dead_ids))


# ── owner checks on a group with no live owner ─────────────────────────────────────────────
#
# A group is left ownerless when nobody on its own team is left to hand it to, and a group whose
# owner was deleted before this rule is read the same way until `prune` has run. An owner-only
# action must not strand such a chat, so on it the owner checks fall back to "any member": but
# only a LIVE member of the chat who is also a live member of the chat's own team. A guest of
# another team (an external chat) is admitted by their own team's managers and never becomes the
# host's owner by this route.


def owner_is_gone(channel: Channel) -> bool:
    """The chat has no owner, or its owner is a deleted account."""
    if channel.owner_id is None:
        return True
    return not CustomUser.objects.filter(pk=channel.owner_id, is_deleted=False).exists()


def host_side_member(channel: Channel, user_id) -> bool:
    """`user_id` is a live member of the chat AND a live member of the chat's own team."""
    return (
        ChannelMember.objects.filter(
            channel=channel, user_id=user_id, is_deleted=False, user__is_deleted=False
        ).exists()
        and TeamMembers.objects.filter(
            team_id=channel.team_id, attendee_id=user_id, is_deleted=False
        ).exists()
    )


def acts_as_owner(channel: Channel, user_id) -> bool:
    """The owner; or, for a group with no live owner, any host-side member."""
    if channel.owner_id is not None and str(channel.owner_id) == str(user_id):
        return True
    if channel.kind != ChannelKind.GM or not owner_is_gone(channel):
        return False
    return host_side_member(channel, user_id)


def actor_gm_role(channel: Channel, user_id) -> str:
    """`resolve_gm_role` for the ACTOR of an owner-gated action: OWNER where `acts_as_owner`.

    Not for the person being acted ON, and not for the leave refusal: there "owner" must stay the
    one real owner, or everyone in an ownerless group would be an owner who cannot be removed
    and cannot leave."""
    from origin.services.member_roles import OWNER, resolve_gm_role

    if acts_as_owner(channel, user_id):
        return OWNER
    return resolve_gm_role(channel, user_id)


def join_request_receiver(channel: Channel):
    """Who a join request to a group is addressed to: the owner, or when the group has no live
    owner its longest-standing host-side member. None when nobody can receive it."""
    if not owner_is_gone(channel):
        return channel.owner_id
    return _successor_id(channel, excluding=channel.owner_id)

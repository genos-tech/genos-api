"""Which chats a person sees in a team: the scoping behind the chat list.

`ChannelListView.get` (the app's own chat list) and the agent's chat tools
(`list_my_chats`, `fetch_chat_thread`) must agree on which channels belong to
a person in a team, so the rule lives here once and both call it. It used to
sit inline in the view; the view keeps its old names as aliases.

Two parts, kept apart on purpose:

  * `user_channels_qs(user)` is membership: the live channels the person holds
    an active `ChannelMember` row on.
  * `team_scope_q(team_id, user_id)` is which of those belong to a team's chat
    list: the team's own channels, the chats another team shared with it that
    this person was admitted to, and the PM chat of a project shared with it.
    It is a `Q` on `Channel` and does not look at membership itself.

And one rule built from them for the agent's chat tools (MIP-917), because
"may this person read this chat, in this team?" was being answered three ways:

  * `readable_chats_qs(team_id, user_id)`: the chats the person is a member of
    (`chat_member_q`, the rule of `agent.acl.chat_acl_user_ids`) AND that are in
    the team's list (`team_scope_q`);
  * `can_read_chat(channel, team_id, user_id)`: the same, for one chat.

`fetch_chat_thread`, the approval card's `readable_channel` and
`list_my_mentions` all go through them.
"""

from __future__ import annotations

from django.db.models import Q

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import ExternalGrant
from origin.models.project.prj_models import ProjectMembers
from origin.services.external_grants import external_objects_by_type


def user_channels_qs(user):
    """Active channels the given user is a member of, with related fields
    needed for serialization preloaded."""
    return (
        Channel.objects.filter(
            members__user=user,
            members__is_deleted=False,
            is_deleted=False,
        )
        .select_related("project", "team", "owner")
        .distinct()
    )


def team_scope_q(team_id, user_id) -> Q:
    """The channels that belong in `user_id`'s chat list for `team_id`.

    `team_id` is a team UUID string, already parsed by the caller. Narrowing
    only: apply it to a queryset that is already limited to the person's own
    memberships (`user_channels_qs`), and naming a team you are not in yields
    nothing rather than anything new.
    """
    # Plus the chats another team shared WITH this one that this person was
    # admitted to. They are the caller's own memberships too, and narrowing by
    # the owning team's id was hiding them from the only sidebar the caller
    # looks at: an external chat showed up solely while viewing the host team,
    # which is not where anyone goes to read their messages.
    #
    # Shared chats and shared projects come from ONE grants lookup: this runs
    # on every chat-list load, and two separate lookups paid for it twice.
    reach = external_objects_by_type(
        (ExternalGrant.ObjectType.CHANNEL, ExternalGrant.ObjectType.PROJECT), team_id, user_id
    )
    shared = reach[ExternalGrant.ObjectType.CHANNEL]
    scope = Q(team_id=team_id)
    if shared:
        scope |= Q(id__in=list(shared.keys()))
    # And the PM chat of a project shared with this team. A project
    # grant admits its guests to the project's own channel (via the
    # `ProjectMembers` -> `ChannelMember` mirror), but nothing named
    # that channel here, so the row stayed out of the guest's
    # sidebar. It was not merely a missing conversation: the PM chat
    # is where a project's avatar lives and the only route into the
    # project profile, so a shared project rendered with no icon and
    # no way to open its settings.
    shared_projects = reach[ExternalGrant.ObjectType.PROJECT]
    if shared_projects:
        scope |= Q(kind=ChannelKind.PM, project_id__in=list(shared_projects.keys()))
    return scope


def chat_member_q(user_id) -> Q:
    """The channels `user_id` is a live member of: a `Q` on `Channel`.

    The rule is `agent.acl.chat_acl_user_ids`, written as a filter so a list
    can apply it in SQL instead of loading a member set per chat:

      * a direct message, group or multi-person DM: an active `ChannelMember` row;
      * a project chat: a `ProjectMembers` row on the channel's project, and NOT
        the channel-member row, which only mirrors it. A stray channel row for
        someone who is not on the project opens nothing.

    Fails closed on no user: `ProjectMembers.attendee` is nullable (a deleted
    user leaves the row behind), and `attendee_id=None` would match it.
    """
    if not user_id:
        return Q(pk__in=[])
    in_channel = ChannelMember.objects.filter(user_id=user_id, is_deleted=False).values(
        "channel_id"
    )
    on_project = ProjectMembers.objects.filter(
        attendee_id=user_id, project_id__isnull=False
    ).values("project_id")
    return (~Q(kind=ChannelKind.PM) & Q(id__in=in_channel)) | (
        Q(kind=ChannelKind.PM) & Q(project_id__in=on_project)
    )


def _readable_chats(user_id, in_team: Q):
    return Channel.objects.filter(chat_member_q(user_id), in_team, is_deleted=False)


def readable_chats_qs(team_id, user_id):
    """The live chats `user_id` may read when working in `team_id`: a member of
    the chat (`chat_member_q`) AND the chat is in the team's chat list
    (`team_scope_q`).

    The one answer to "may this person read this chat, here?" for the agent's
    chat tools: `fetch_chat_thread` and the approval card's
    `LabelAccess.readable_channel` ask it of one chat (`can_read_chat`), and
    `list_my_mentions` applies it to the chats its mentions are in. A fourth
    reader should call this too, not write the rule again.
    """
    return _readable_chats(user_id, team_scope_q(team_id, user_id))


def can_read_chat(channel: Channel, team_id, user_id) -> bool:
    """`readable_chats_qs` for one chat already looked up.

    Takes the row, not an id, so the caller can tell a chat that is not there
    from one that is and is refused (`fetch_chat_thread` answers them with
    different exceptions).
    """
    # The team's own chat is in its list by `team_scope_q`'s first clause, so
    # the grant lookups are skipped for it: the common case, and the only one
    # that needs no query at all to place the chat in the team.
    if str(channel.team_id) == str(team_id):
        in_team = Q(team_id=team_id)
    else:
        in_team = team_scope_q(team_id, user_id)
    return _readable_chats(user_id, in_team).filter(id=channel.id).exists()

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

from typing import Any

from django.db.models import F, Q

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import ExternalGrant
from origin.models.project.prj_models import ProjectMembers
from origin.services.external_grants import external_objects_by_type


def user_channels_qs(user):
    """Live channels the given user is a member of (`chat_member_q`), with related
    fields needed for serialization preloaded.

    `user` is a user or a user id. The membership rule is the product's one (MIP-1480): a
    project chat is the project's people, so a stray channel-member row opens nothing.
    `chat_member_q` filters with subqueries, so there is no join and no duplicate to
    `distinct()` away.
    """
    return Channel.objects.filter(
        chat_member_q(getattr(user, "id", user)), is_deleted=False
    ).select_related("project", "team", "owner")


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


def chat_note_member_q(user_id) -> Q:
    """The chat notes `user_id` may open by being in their chat: a `Q` on `ChatNoteMaster`.

    The ONE rule for "may this person read this chat note through its chat?" (MIP-1474), shared
    by `note_role.get_effective_role` (REST, the collab gate, every write gate), its batch
    `readable_note_ids` (favorites, recents, the timeline), the sidebar's note list and the
    briefing. The search index's `chat_acl_user_ids` (and so `get_note` over MCP and in-app) is
    the same rule written as a set of user ids; `test_timeline_note_rule` pins them equal over a generated matrix.

    A note opens when its channel is live, the channel's kind is the `chat_type` the note was
    filed under, and the person is a member of that channel by `chat_member_q` (a project chat is
    read by whoever is ON THE PROJECT: its channel-member row only mirrors that, and a stray row
    opens nothing). An explicit grant or the note's owner are the note's own ACL, not the chat's:
    callers add them.

    Fails closed on no user, as `chat_member_q` does.
    """
    live_and_mine = Channel.objects.filter(chat_member_q(user_id), is_deleted=False).values("id")
    return Q(channel_id__in=live_and_mine) & Q(chat_type=F("channel__kind"))


def is_chat_member(channel_id, user_id) -> bool:
    """Whether `user_id` is a member of the chat `channel_id` by `chat_member_q`.

    The membership half of `can_read_chat`, for the REST gates that scope by membership alone
    (the channel, message, reaction, flag, pin and media endpoints): they answer "not a member"
    exactly as they answer "no such chat". Does not look at `is_deleted` (the callers that need a
    live chat already fetched one) or at the team.
    """
    return Channel.objects.filter(chat_member_q(user_id), id=channel_id).exists()


def chat_member_rows(channel: Channel):
    """The live `ChannelMember` rows that count as members of `channel`: a roster.

    A direct message, group or multi-person DM: its live rows. A project chat: only the rows of
    people who are ON THE PROJECT, because a stray row (a person who is not on the project) is
    not a member by `chat_member_q`, so it must not be listed in the roster, mentioned into the
    chat, or notified of its messages.

    A DELETED ACCOUNT is never listed, even where its row is kept (a direct message or a
    multi-person DM keeps the row so the others keep the conversation, MIP-1546): this is the
    roster that OFFERS people (the roster endpoints, `list_channel_members`, the mention check,
    the notification fan-out), and a person who no longer exists is not one to mention, add or
    notify. A group or project row of an account deleted before that change is hidden here too.
    The chat list's own DM/MDM identity roster is NOT this helper: it keeps the row so the chat
    still names its other side "Deleted user".
    """
    rows = ChannelMember.objects.filter(channel=channel, is_deleted=False, user__is_deleted=False)
    if channel.kind == ChannelKind.PM:
        rows = rows.filter(
            user_id__in=ProjectMembers.objects.filter(
                project_id=channel.project_id, attendee_id__isnull=False
            ).values("attendee_id")
        )
    return rows


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


# How many other people's names a multi-person DM's title lists.
MDM_NAMES = 5


def channel_titles(channels: list, user_id: str) -> dict[Any, str]:
    """Readable name per channel id, as the app names a chat. A stored title wins; a DM or
    multi-person DM has none and is named after the other people (a DM with yourself, after
    you; a multi-person DM lists up to `MDM_NAMES` of them and counts the rest). A group or
    project chat with no title has no name: "".

    The one rule for "what is this chat called" outside the app's own list: `list_my_chats`
    and the `unattached` chats of an item line both call it, so a DM is never named two ways.
    One query for all the unnamed DMs, however many channels are given."""
    unnamed = [
        c.id for c in channels if not c.title and c.kind in (ChannelKind.DM, ChannelKind.MDM)
    ]
    people: dict[Any, list[tuple[Any, str]]] = {}
    if unnamed:
        for m in (
            ChannelMember.objects.filter(channel_id__in=unnamed, is_deleted=False)
            .select_related("user")
            .order_by("ts_joined_at")
        ):
            people.setdefault(m.channel_id, []).append((m.user_id, (m.user.username or "")))
    out: dict[Any, str] = {}
    for c in channels:
        if c.title:
            out[c.id] = c.title
            continue
        members = people.get(c.id, [])
        others = [name for uid, name in members if str(uid) != user_id and name]
        if c.kind == ChannelKind.DM:
            # The partner; in a chat with yourself, you.
            out[c.id] = others[0] if others else (members[0][1] if members else "")
        elif c.kind == ChannelKind.MDM:
            shown = ", ".join(others[:MDM_NAMES])
            more = len(others) - MDM_NAMES
            out[c.id] = f"{shown} +{more} more" if more > 0 else shown
        else:
            out[c.id] = ""
    return out

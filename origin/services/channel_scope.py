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
"""

from __future__ import annotations

from django.db.models import Q

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.team_models import ExternalGrant
from origin.services.external_grants import external_objects_for_member


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
    shared = external_objects_for_member(ExternalGrant.ObjectType.CHANNEL, team_id, user_id)
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
    shared_projects = external_objects_for_member(
        ExternalGrant.ObjectType.PROJECT, team_id, user_id
    )
    if shared_projects:
        scope |= Q(kind=ChannelKind.PM, project_id__in=list(shared_projects.keys()))
    return scope

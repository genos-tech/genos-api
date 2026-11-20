"""Put people into the team's default chat and their department's chat (MIP-1708).

`ChannelAutoJoin` marks two kinds of open chat (`models/chat/auto_join_models`): the team's
default chat (`general`), joined by everybody who joins the team, and a department's chat, joined
by the members of that department's mention group. This module is the ONE place that adds
people to them. It is called, never wired as a signal: tests, fixtures, the demo seeders and the
evals create members in bulk, and none of them is somebody joining a team.

Called from every real join (`add_team_member`, i.e. an accepted invite; `TeamMembersView.post`,
a direct join; `JoinTeamFromInboxView`, an approved request) through `join_default_chats`, and
from `MentionGroupMembersView.post` through `add_group_members_to_chat`.

Who is added, and who is never:

* Only a LIVE, non-guest member of the chat's team (a guest holds no `TeamMembers` row; the role
  check is belt and braces), whose account is not deleted and is not a system user.
* Only to a chat that is not deleted, not private, a group chat (`GM`) and not external. A rule
  on a chat that has since been made private or shared outside the team simply stops adding.
* Never a person who has a `ChannelMember` row of ANY state. A live row is "already in"; a
  soft-deleted row is "left", and a person who left is not put back (Ken, 2026-11-20). That also
  covers someone who left the TEAM and rejoins: leaving the team soft-deletes every channel row
  of the team (`drop_in_team_access`), which is indistinguishable from leaving the chat without
  a new column, so they are not put back either and find the chat by name, as everybody did
  before this existed. Re-joiners lose their other chats the same way.
* The role is plain `member`.

Removal from a mention group never removes the person from the chat: the group is how they were
let in, not what keeps them in; a chat is left by leaving it.

Best-effort, like the seeders: a failure (logged with the chat and user ids, never content) is
swallowed in a savepoint and never fails the join or the group change. The live notice goes only
to the people who were added, after the commit (`push_channel_appeared`).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q

from origin.models.chat.auto_join_models import ChannelAutoJoin
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.mention_group_models import MentionGroupMembers
from origin.models.common.team_models import TeamMembers
from origin.services.member_roles import GUEST
from origin.services.realtime_push import push_channel_appeared

log = logging.getLogger(__name__)


def mark_default_chat(team, channel) -> ChannelAutoJoin:
    """Make `channel` the chat everybody who joins `team` is put into."""
    return ChannelAutoJoin.objects.create(team=team, channel=channel, mention_group=None)


def mark_department_chat(team, channel, group) -> ChannelAutoJoin:
    """Make `channel` the chat the members of mention group `group` are put into."""
    return ChannelAutoJoin.objects.create(team=team, channel=channel, mention_group=group)


def _open_rules():
    """The rules whose chat can be joined by being added: open, live, internal group chats of a
    live team."""
    return ChannelAutoJoin.objects.filter(
        channel__is_deleted=False,
        channel__is_private=False,
        channel__is_external=False,
        channel__kind=ChannelKind.GM,
        team__is_deleted=False,
    ).select_related("channel")


def eligible_user_ids(team_id, user_ids) -> list:
    """Of `user_ids`, those who may be put into a chat of `team_id`: live non-guest members of
    the team whose account is a real, live one."""
    return list(
        TeamMembers.objects.filter(
            team_id=team_id,
            attendee_id__in=list(user_ids),
            is_deleted=False,
            attendee__is_deleted=False,
            attendee__is_system_user=False,
        )
        .exclude(member_role=GUEST)
        .values_list("attendee_id", flat=True)
    )


def _put_in(channel, user_ids, *, announce: bool) -> list:
    """Give each of `user_ids` a `member` row in `channel`, unless they have one of any state.
    One savepoint per person, so one failure costs that person only. Returns who was added."""
    added = []
    for user_id in user_ids:
        try:
            with transaction.atomic():
                _row, created = ChannelMember.objects.get_or_create(
                    channel=channel, user_id=user_id, defaults={"role": "member"}
                )
        except Exception:  # noqa: BLE001 — the join / group change must not fail
            log.exception(
                "default chat join failed channel=%s user=%s team=%s",
                channel.pk,
                user_id,
                channel.team_id,
            )
            continue
        if created:
            added.append(user_id)
    if added and announce:
        # Only the people added, never the roster. Sent after the commit; a rollback sends none.
        push_channel_appeared(
            user_ids=added,
            team_id=channel.team_id,
            channel_id=channel.pk,
            kind=channel.kind,
        )
    return added


def join_default_chats(team_id, user_id, *, announce: bool = True) -> list:
    """`user_id` has just joined `team_id` (a new membership row, or an un-deleted one): put them
    into the team's default chat and into the chats of the mention groups they are in.

    Call it inside the transaction that wrote the membership. Returns the ids of the chats they
    were added to (empty when there is nothing to do: no default chat, a person who is not
    eligible, a chat they left). Never raises. `announce=False` when the caller's own relay has
    already told the person's apps (`announced_by_sockets`).
    """
    try:
        with transaction.atomic():
            if not eligible_user_ids(team_id, [user_id]):
                return []
            in_groups = MentionGroupMembers.objects.filter(
                user_id=user_id, group__is_deleted=False
            ).values("group_id")
            rules = _open_rules().filter(
                Q(team_id=team_id),
                Q(mention_group__isnull=True) | Q(mention_group_id__in=in_groups),
            )
            joined = []
            for rule in rules.order_by("id"):
                if _put_in(rule.channel, [user_id], announce=announce):
                    joined.append(str(rule.channel_id))
            return joined
    except Exception:  # noqa: BLE001 — the join must not fail
        log.exception("default chats could not be joined user=%s team=%s", user_id, team_id)
        return []


def add_group_members_to_chat(group, user_ids, *, announce: bool = True) -> dict:
    """`user_ids` were just added to mention group `group`: put them into the group's chat (a
    department's), if it has one. Returns `{chat id: [user ids added]}`. Never raises."""
    try:
        if group.is_deleted or group.team_id is None:
            return {}
        with transaction.atomic():
            people = eligible_user_ids(group.team_id, user_ids)
            if not people:
                return {}
            result = {}
            for rule in _open_rules().filter(mention_group_id=group.group_id).order_by("id"):
                added = _put_in(rule.channel, people, announce=announce)
                if added:
                    result[str(rule.channel_id)] = [str(u) for u in added]
            return result
    except Exception:  # noqa: BLE001 — the group change must not fail
        log.exception("group chat could not be joined group=%s", getattr(group, "group_id", None))
        return {}


class AdoptionReport:
    """What `adopt_general_chats` found: `marked` (team id, chat id), `ambiguous` (team id, how
    many open `general` chats), `already` (teams that have a default chat), `added` (people put
    into a chat) and `without_chat` (teams with no open `general`)."""

    def __init__(self):
        self.marked: list[tuple[str, str]] = []
        self.ambiguous: list[tuple[str, int]] = []
        self.already = 0
        self.without_chat = 0
        self.added = 0


def adopt_general_chats(*, apply: bool, add_members: bool = False, team_id=None) -> AdoptionReport:
    """Mark an EXISTING team's open `general` chat as its default chat, and optionally put every
    current eligible member into it (MIP-1708; never run by a migration or a deploy).

    A team is marked only when it has no default chat yet and exactly one open, live group chat
    titled `general` (the title `team_setup` gives it): zero is reported, more than one is
    reported as ambiguous and left alone. People who left the chat are skipped like everywhere
    else. `apply=False` counts and writes nothing.
    """
    from origin.models.common.team_models import TeamMaster
    from origin.services.team_setup import GENERAL_TITLE

    report = AdoptionReport()
    teams = TeamMaster.objects.filter(is_deleted=False)
    if team_id:
        teams = teams.filter(team_id=team_id)
    for team in teams.order_by("ts_created_at"):
        if ChannelAutoJoin.objects.filter(team=team, mention_group__isnull=True).exists():
            report.already += 1
            continue
        chats = list(
            Channel.objects.filter(
                team=team,
                kind=ChannelKind.GM,
                title=GENERAL_TITLE,
                is_deleted=False,
                is_private=False,
                is_external=False,
                auto_join__isnull=True,
            )
        )
        if not chats:
            report.without_chat += 1
            continue
        if len(chats) > 1:
            report.ambiguous.append((str(team.team_id), len(chats)))
            continue
        chat = chats[0]
        report.marked.append((str(team.team_id), str(chat.pk)))
        if not apply:
            continue
        with transaction.atomic():
            mark_default_chat(team, chat)
            if add_members:
                people = eligible_user_ids(
                    team.team_id,
                    TeamMembers.objects.filter(team=team, is_deleted=False).values_list(
                        "attendee_id", flat=True
                    ),
                )
                report.added += len(_put_in(chat, people, announce=True))
    return report

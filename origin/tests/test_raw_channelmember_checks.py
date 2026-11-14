"""MIP-1495: the last readers of the raw `ChannelMember` row use the ONE chat-membership rule.

`channel_scope.chat_member_q` / `is_chat_member` / `chat_member_rows` say who is a member of a
chat: a project (PM) chat is the PROJECT's people, its channel-member row only mirrors that, so a
stray row (someone not on the project) opens nothing. Three readers still asked the raw row:

  * `v3_activity.create_task_assign_activity`: wrote "assigned you a task" onto a PM card for a
    stray row;
  * `mention_membership._members_of_channel`: the "add to chat?" prompt counted a stray row as a
    member (so it never offered to add the person, who in fact cannot open the chat);
  * `personal_tag_views` (the tag bundle): joined assignments on the raw row.

Every one of them is asked the same questions below.
"""

from __future__ import annotations

from django.urls import reverse

from origin.models.chat.personal_tag_models import (
    PersonalChannelTag,
    PersonalChannelTagAssignment,
)
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.tests.test_task_assignment_notice import AssignmentNoticeBase
from origin.views.utils.mention_membership import (
    SCOPE_CHANNEL,
    _members_of_channel,
    non_member_mentions,
)


class RawChannelMemberChecks(AssignmentNoticeBase):
    """`self.channel` is the PM chat of `self.project`; user, user2, user3 are on the project."""

    def setUp(self):
        super().setUp()
        self.stray = CustomUser.objects.create_user(
            email="stray@example.com", username="stray", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stray)
        # a channel row for someone who is NOT on the project
        ChannelMember.objects.update_or_create(
            channel=self.channel, user=self.stray, defaults={"is_deleted": False}
        )
        # a project member whose channel row is gone (the mirror is not the rule)
        ChannelMember.objects.filter(channel=self.channel, user=self.user3).delete()

    # -- v3_activity: the task-assign mention ---------------------------------------------- #

    def make_notice(self, recipient):
        from origin.services import v3_activity

        return v3_activity.create_task_assign_activity(
            message=self.card, recipient_id=recipient.id, actor=self.user
        )

    def test_a_stray_row_on_a_project_chat_is_not_told_of_an_assignment(self):
        self.assertEqual(self.make_notice(self.stray), [])

    def test_a_project_member_is_told_whether_or_not_the_mirror_row_exists(self):
        self.assertEqual(len(self.make_notice(self.user3)), 1)
        self.assertEqual(len(self.make_notice(self.user2)), 1)

    # -- mention_membership: the "add to chat?" prompt ------------------------------------- #

    def test_the_roster_of_a_project_chat_is_the_projects_people(self):
        self.assertEqual(
            _members_of_channel(self.channel.id),
            {str(self.user.id), str(self.user2.id), str(self.user3.id)},
        )

    def test_the_add_to_chat_prompt_flags_a_stray_row_and_not_a_project_member(self):
        out = non_member_mentions(
            [self.stray.id, self.user3.id, self.user2.id], channel_id=self.channel.id
        )
        self.assertEqual(out["scopeKind"], SCOPE_CHANNEL)
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.stray.id)])

    def test_a_live_group_chat_row_is_still_a_member_and_a_removed_one_is_not(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=gm, user=self.user2)
        ChannelMember.objects.create(channel=gm, user=self.user3, is_deleted=True)
        self.assertEqual(_members_of_channel(gm.id), {str(self.user2.id)})
        self.assertEqual(_members_of_channel(None), set())

    # -- personal tags: the bundle ---------------------------------------------------------- #

    def tag_bundle_assignments(self, user):
        self.authenticate(user)
        res = self.client.get(reverse("v3_personal_tag_list"))
        self.assertEqual(res.status_code, 200, res.content)
        return res.json().get("assignments", {})

    def test_the_tag_bundle_drops_an_assignment_on_a_chat_the_user_is_not_a_member_of(self):
        tag = PersonalChannelTag.objects.create(
            user=self.stray, name="t", color="#fff", text_color="black"
        )
        PersonalChannelTagAssignment.objects.create(tag=tag, channel=self.channel)
        self.assertEqual(self.tag_bundle_assignments(self.stray), {})

    def test_the_tag_bundle_keeps_an_assignment_on_a_group_chat_the_user_is_in(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=gm, user=self.stray)
        tag = PersonalChannelTag.objects.create(
            user=self.stray, name="t", color="#fff", text_color="black"
        )
        PersonalChannelTagAssignment.objects.create(tag=tag, channel=gm)
        self.assertEqual(self.tag_bundle_assignments(self.stray), {str(gm.id): [tag.tag_id]})

    def test_the_tag_bundle_drops_a_group_chat_the_user_left(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=gm, user=self.stray, is_deleted=True)
        tag = PersonalChannelTag.objects.create(
            user=self.stray, name="t", color="#fff", text_color="black"
        )
        PersonalChannelTagAssignment.objects.create(tag=tag, channel=gm)
        self.assertEqual(self.tag_bundle_assignments(self.stray), {})

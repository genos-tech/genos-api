"""ACL tests for the membership-roster tools (A1-ext).

`list_project_members` / `list_channel_members` enumerate people —
exactly the kind of payload where a tenant or membership hole leaks
organisational structure. Contract under test:

  * cross-team access → ToolError (tenant guard);
  * a requester who isn't themselves on the roster → ToolError
    (membership guard — non-members may not enumerate);
  * a member gets the roster, minus soft-deleted memberships and
    deleted/system users;
  * no emails unless the call asks for them, since every result goes to
    the LLM provider (MIP-622).
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.agent.tools.list_channel_members import LIST_CHANNEL_MEMBERS
from origin.search_engine.agent.tools.list_project_members import LIST_PROJECT_MEMBERS

from .test_base import BaseAPITestCase

User = get_user_model()


class ListProjectMembersTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_member_gets_the_roster(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        out = LIST_PROJECT_MEMBERS.run({"project_id": self.project.project_id}, self.ctx)
        names = {m["username"] for m in out["members"]}
        self.assertEqual(names, {"testuser", "otheruser"})
        self.assertIn("2 member(s)", out["__summary__"])

    def test_emails_only_when_asked(self):
        args = {"project_id": self.project.project_id}
        out = LIST_PROJECT_MEMBERS.run(args, self.ctx)
        self.assertNotIn("email", out["members"][0])
        self.assertNotIn(self.user.email, repr(out))
        # Only a real true: a model's "yes" is not a request.
        out = LIST_PROJECT_MEMBERS.run({**args, "include_email": "yes"}, self.ctx)
        self.assertNotIn("email", out["members"][0])
        out = LIST_PROJECT_MEMBERS.run({**args, "include_email": True}, self.ctx)
        self.assertEqual(out["members"][0]["email"], self.user.email)

    def test_non_member_is_denied(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_PROJECT_MEMBERS.run({"project_id": self.project.project_id}, ctx2)

    def test_cross_team_is_denied(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ctx_other = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "different team"):
            LIST_PROJECT_MEMBERS.run({"project_id": self.project.project_id}, ctx_other)

    def test_deleted_and_system_users_are_hidden(self):
        ghost = User.objects.create_user(username="ghost", email="ghost@example.com", password="x")
        ghost.is_deleted = True
        ghost.save(update_fields=["is_deleted"])
        bot = User.objects.create_user(username="bot", email="bot@example.com", password="x")
        bot.is_system_user = True
        bot.save(update_fields=["is_system_user"])
        for u in (ghost, bot):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=u)
        out = LIST_PROJECT_MEMBERS.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual({m["username"] for m in out["members"]}, {"testuser"})

    def test_bad_id_and_missing_project(self):
        with self.assertRaisesMessage(ToolError, "must be an integer"):
            LIST_PROJECT_MEMBERS.run({"project_id": "abc"}, self.ctx)
        with self.assertRaisesMessage(ToolError, "not found"):
            LIST_PROJECT_MEMBERS.run({"project_id": 999999}, self.ctx)


class GetTeamMembersEmailTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_emails_only_when_asked(self):
        out = GET_TEAM_MEMBERS.run({}, self.ctx)
        self.assertTrue(out["members"])
        self.assertTrue(all("email" not in m for m in out["members"]))
        self.assertNotIn(self.user.email, repr(out))
        out = GET_TEAM_MEMBERS.run({"include_email": True}, self.ctx)
        self.assertIn(self.user.email, {m["email"] for m in out["members"]})


class ListChannelMembersTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(team=self.team, kind=2, title="general")
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_member_gets_the_roster_with_roles(self):
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, self.ctx)
        by_name = {m["username"]: m["role"] for m in out["members"]}
        self.assertEqual(by_name, {"testuser": "owner", "otheruser": "member"})
        self.assertEqual(out["channel_kind"], "gm")

    def test_non_member_is_denied(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, ctx2)

    def test_soft_deleted_membership_is_hidden_and_denies(self):
        # A left member neither appears in the roster nor may list it.
        ChannelMember.objects.create(
            channel=self.channel, user=self.user2, role="member", is_deleted=True
        )
        out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, self.ctx)
        self.assertEqual({m["username"] for m in out["members"]}, {"testuser"})
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, ctx2)

    def test_cross_team_is_denied(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ctx_other = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user2.id))
        # Answered as a channel that is not there (MIP-885), so the reply does not
        # say that it exists in another team.
        with self.assertRaisesMessage(ToolError, "not found, or you are not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, ctx_other)

    def test_bad_uuid_and_missing_channel(self):
        with self.assertRaisesMessage(ToolError, "must be a UUID"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": "not-a-uuid"}, self.ctx)
        with self.assertRaisesMessage(ToolError, "not found"):
            LIST_CHANNEL_MEMBERS.run(
                {"channel_id": "00000000-0000-4000-8000-000000000099"}, self.ctx
            )


class ListChannelMembersProjectChatTests(BaseAPITestCase):
    """MIP-1501: a project (PM) chat is the PROJECT's people (`channel_scope.chat_member_q`); its
    channel-member row only mirrors that, so a stray row opens nothing and is not listed."""

    def setUp(self):
        super().setUp()
        self.stray = User.objects.create_user(
            username="stray", email="stray@example.com", password="x"
        )
        self.member = User.objects.create_user(
            username="pm-member", email="pmm@example.com", password="x"
        )
        for u in (self.stray, self.member):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PMX",
            owner=self.user,
            project_system_user=self.user,
        )
        for m in (self.user, self.member):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=m)
        self.chat = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        # The stray row: nothing on the project backs it.
        ChannelMember.objects.create(channel=self.chat, user=self.stray, role="member")

    def ctx(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def roster(self, user):
        out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.chat.id)}, self.ctx(user))
        return {m["username"] for m in out["members"]}, out

    def test_a_stray_row_gets_the_same_refusal_as_a_missing_chat(self):
        missing = "00000000-0000-4000-8000-000000000099"
        with self.assertRaises(ToolError) as got:
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.chat.id)}, self.ctx(self.stray))
        with self.assertRaises(ToolError) as gone:
            LIST_CHANNEL_MEMBERS.run({"channel_id": missing}, self.ctx(self.stray))
        self.assertEqual(
            str(got.exception).replace(str(self.chat.id), "<id>"),
            str(gone.exception).replace(missing, "<id>"),
        )

    def test_a_project_member_gets_the_project_not_the_stray(self):
        names, out = self.roster(self.member)
        self.assertEqual(names, {self.user.username, "pm-member"})
        self.assertNotIn("stray", repr(out))
        self.assertEqual(out["channel_kind"], "pm")
        self.assertIn("2 member(s)", out["__summary__"])

    def test_a_project_member_without_a_channel_row_still_reads_the_roster(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.member).delete()
        names, _ = self.roster(self.member)
        self.assertEqual(names, {self.user.username})  # the row is what lists a person

    def test_leaving_the_project_closes_the_roster(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.member).delete()
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.chat.id)}, self.ctx(self.member))

    def test_a_group_chat_still_goes_by_its_channel_member_row(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        for u in (self.user, self.stray):
            ChannelMember.objects.create(channel=gm, user=u, role="member")
        out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(gm.id)}, self.ctx(self.stray))
        self.assertEqual({m["username"] for m in out["members"]}, {self.user.username, "stray"})

    def test_a_direct_message_still_goes_by_its_channel_member_row(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM)
        for u in (self.user, self.member):
            ChannelMember.objects.create(channel=dm, user=u, role="member")
        out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(dm.id)}, self.ctx(self.member))
        self.assertEqual({m["username"] for m in out["members"]}, {self.user.username, "pm-member"})
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(dm.id)}, self.ctx(self.stray))

    def test_an_external_guest_on_the_project_is_refused_by_the_team_guard_as_before(self):
        # A guest works from their own team; the tool is stricter than the chat list (tenant guard).
        guest_team = TeamMaster.objects.create(
            team_name="Guest", team_email="guest-team@example.com", owner=self.stray
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.stray)
        ctx = ToolContext(team_id=str(guest_team.team_id), user_id=str(self.stray.id))
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.chat.id)}, ctx)

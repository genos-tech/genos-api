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

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.common.team_models import TeamMaster
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
        ghost = User.objects.create_user(
            username="ghost", email="ghost@example.com", password="x"
        )
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
        with self.assertRaisesMessage(ToolError, "different team"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": str(self.channel.id)}, ctx_other)

    def test_bad_uuid_and_missing_channel(self):
        with self.assertRaisesMessage(ToolError, "must be a UUID"):
            LIST_CHANNEL_MEMBERS.run({"channel_id": "not-a-uuid"}, self.ctx)
        with self.assertRaisesMessage(ToolError, "not found"):
            LIST_CHANNEL_MEMBERS.run(
                {"channel_id": "00000000-0000-4000-8000-000000000099"}, self.ctx
            )

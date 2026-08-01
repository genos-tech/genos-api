"""What a guest may see of the people around them.

This is the requirement §4.1 of the readiness plan actually names: *"the
hard part is auditing every list endpoint so a guest can't enumerate the
whole team."* Three surfaces answer "who exists?" and all three have to
agree:

  getMyTeams          the team shell the client boots into
  getTeamMembers      the roster behind avatars and assignee pickers
  v3 teamMembersAndGroups   the @-mention autocomplete source

A guest sees the members of the projects they were invited to, and
nobody else. `bystander` below is a full team member who shares no
project with the guest, and is the control: they must be invisible on
every surface while remaining visible to real members.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import GUEST
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

MY_TEAMS = "/api/v2/team/getMyTeams/"
TEAM_MEMBERS = "/api/v2/team/getTeamMembers/"
MENTION_SOURCE = "/api/v3/search/teamMembersAndGroups/"


class GuestRosterBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # self.user and self.user2 are already team members.
        self.shared_project = ProjectMaster.objects.create(
            team=self.team, project_name="Shared With Client", owner=self.user
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.shared_project, attendee=self.user
        )

        # A full team member who shares NO project with the guest.
        self.bystander = User.objects.create_user(
            username="bystander", email="bystander@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.bystander)

        # The guest: in the shared project only, no TeamMembers row.
        self.guest = User.objects.create_user(
            username="theguest", email="theguest@agency.example", password="pw"
        )
        ProjectMembers.objects.create(
            team=self.team,
            project=self.shared_project,
            attendee=self.guest,
            member_role=GUEST,
        )


class TestGuestSeesTheTeamShell(GuestRosterBase):
    def test_guest_gets_the_team_so_the_client_can_boot(self):
        """A guest has no TeamMembers row, so without this branch
        getMyTeams returns [] and the client has nowhere to render."""
        self.authenticate(self.guest)
        res = self.client.get(MY_TEAMS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual({str(t["teamId"]) for t in res.data}, {str(self.team.team_id)})

    def test_the_shell_carries_only_shared_people(self):
        self.authenticate(self.guest)
        res = self.client.get(MY_TEAMS)
        emails = {m["userEmail"] for m in res.data[0]["teamMembers"]}
        self.assertIn(self.user.email, emails)
        self.assertNotIn(self.bystander.email, emails)
        self.assertNotIn(self.user2.email, emails)

    def test_a_real_member_still_sees_everyone(self):
        self.authenticate(self.user2)
        res = self.client.get(MY_TEAMS)
        emails = {m["userEmail"] for m in res.data[0]["teamMembers"]}
        self.assertIn(self.bystander.email, emails)

    def test_a_stranger_still_sees_nothing(self):
        stranger = User.objects.create_user(
            username="rosterstranger", email="rosterstranger@example.com", password="pw"
        )
        self.authenticate(stranger)
        res = self.client.get(MY_TEAMS)
        self.assertEqual(res.data, [])


class TestGuestRoster(GuestRosterBase):
    def test_guest_roster_excludes_unshared_members(self):
        self.authenticate(self.guest)
        res = self.client.get(TEAM_MEMBERS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        emails = {m["userEmail"] for m in res.data["data"]["members"]}
        self.assertIn(self.user.email, emails)
        self.assertNotIn(self.bystander.email, emails)

    def test_guest_of_another_team_is_still_refused(self):
        from origin.models.common.team_models import TeamMaster

        other_owner = User.objects.create_user(
            username="rosterother", email="rosterother@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Roster Other", team_email="rosterother@example.com", owner=other_owner
        )
        self.authenticate(self.guest)
        res = self.client.get(TEAM_MEMBERS, {"team_id": str(other_team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_member_roster_is_unchanged(self):
        self.authenticate(self.user2)
        res = self.client.get(TEAM_MEMBERS, {"team_id": str(self.team.team_id)})
        emails = {m["userEmail"] for m in res.data["data"]["members"]}
        self.assertIn(self.bystander.email, emails)


class TestGuestMentionSource(GuestRosterBase):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Internal GM", owner=self.user
        )

    def test_guest_mention_list_excludes_unshared_members(self):
        """Autocomplete is the easiest way to enumerate a directory."""
        self.authenticate(self.guest)
        res = self.client.get(MENTION_SOURCE, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        people = {r["email"] for r in res.data["results"] if r["type"] == "People"}
        self.assertIn(self.user.email, people)
        self.assertNotIn(self.bystander.email, people)

    def test_guest_sees_no_group_channels_at_all(self):
        """The GM list is team-wide and includes PRIVATE channels — the
        richest disclosure on this endpoint."""
        self.authenticate(self.guest)
        res = self.client.get(MENTION_SOURCE, {"team_id": str(self.team.team_id)})
        groups = [r for r in res.data["results"] if r["type"] == "Group"]
        self.assertEqual(groups, [])

    def test_a_member_still_sees_group_channels(self):
        self.authenticate(self.user2)
        res = self.client.get(MENTION_SOURCE, {"team_id": str(self.team.team_id)})
        groups = {r["name"] for r in res.data["results"] if r["type"] == "Group"}
        self.assertIn("Internal GM", groups)

    def test_a_stranger_is_still_404(self):
        stranger = User.objects.create_user(
            username="mentionstranger", email="mentionstranger@example.com", password="pw"
        )
        self.authenticate(stranger)
        res = self.client.get(MENTION_SOURCE, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)


class TestGuestInTwoTeams(GuestRosterBase):
    def test_full_member_in_one_team_and_guest_in_another(self):
        """The narrowing is per-team, not per-user: being a guest
        somewhere must not shrink the roster where you're a member."""
        from origin.models.common.team_models import TeamMaster

        home = TeamMaster.objects.create(
            team_name="Guest Home Team", team_email="guesthome@example.com", owner=self.guest
        )
        TeamMembers.objects.create(team=home, attendee=self.guest)
        colleague = User.objects.create_user(
            username="colleague", email="colleague@agency.example", password="pw"
        )
        TeamMembers.objects.create(team=home, attendee=colleague)

        self.authenticate(self.guest)
        res = self.client.get(MY_TEAMS)
        by_team = {str(t["teamId"]): t for t in res.data}
        self.assertEqual(len(by_team), 2)

        # Full roster in their own team...
        home_emails = {m["userEmail"] for m in by_team[str(home.team_id)]["teamMembers"]}
        self.assertIn(colleague.email, home_emails)
        # ...narrow roster in the team they're a guest in.
        client_emails = {m["userEmail"] for m in by_team[str(self.team.team_id)]["teamMembers"]}
        self.assertNotIn(self.bystander.email, client_emails)

"""Leaving a team gives up what that team's membership underwrote in it.

The outward half of the cascade — access a departing member held in OTHER
teams through a share — is covered by `test_external_grants`. This suite
is the near half: the team's own projects, channels and note folders each
keep their own membership row, none of which is `TeamMembers`, so a
departure that only flipped `TeamMembers.is_deleted` left every one of
them granting what it always granted. The project rows were the loudest
symptom: `GetMyTeamsView` builds a guest shell for any team the caller
holds a `ProjectMembers` row in, so the team someone just left came back
in their switcher with its projects still open.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.project.prj_models import ProjectMembers
from origin.services.external_grants import add_external_participants
from origin.services.team_membership import remove_team_member
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import ROLE_EDITOR


class LeaveTeamInTeamCascadeTests(CrossTeamTestCase):
    """`self.a_viewer` is a plain member of team A and does the leaving."""

    def setUp(self):
        super().setUp()
        self.leaver = self.a_viewer
        self.stayer = self.a_editor

        for user in (self.leaver, self.stayer):
            ProjectMembers.objects.create(
                team=self.team_a, project=self.project, attendee=user, member_role="viewer"
            )

        self.gm = Channel.objects.create(
            team=self.team_a,
            kind=ChannelKind.GM,
            title="Team A room",
            owner=self.a_owner,
        )
        for user in (self.leaver, self.stayer):
            ChannelMember.objects.create(channel=self.gm, user=user)
            NoteFolderPermission.objects.create(
                team=self.team_a, folder=self.folder, user=user, role_id=ROLE_EDITOR
            )

    def _channel_row(self, user):
        return ChannelMember.objects.get(channel=self.gm, user=user)

    def test_the_teams_projects_go(self):
        remove_team_member(self.team_a.team_id, self.leaver.id)
        self.assertFalse(self._in_project(self.leaver))

    def test_the_teams_channels_go(self):
        remove_team_member(self.team_a.team_id, self.leaver.id)
        # Soft, because that is this table's convention — the re-join paths
        # un-delete the row in place rather than inserting a second one.
        self.assertTrue(self._channel_row(self.leaver).is_deleted)

    def test_the_teams_note_folders_go(self):
        remove_team_member(self.team_a.team_id, self.leaver.id)
        self.assertFalse(
            NoteFolderPermission.objects.filter(
                team=self.team_a, folder=self.folder, user=self.leaver
            ).exists()
        )
        # The row is what a private team folder resolves through, so this
        # is the access itself and not just the bookkeeping.
        self.assertIsNone(get_folder_role(self.leaver.id, self.folder.folder_id))

    def test_everyone_who_stayed_keeps_everything(self):
        remove_team_member(self.team_a.team_id, self.leaver.id)
        self.assertTrue(self._in_project(self.stayer))
        self.assertFalse(self._channel_row(self.stayer).is_deleted)
        self.assertEqual(get_folder_role(self.stayer.id, self.folder.folder_id), ROLE_EDITOR)

    def test_another_teams_projects_are_none_of_this_departures_business(self):
        # Their own membership elsewhere, which leaving team A cannot touch.
        TeamMembers.objects.create(team=self.team_c, attendee=self.leaver)
        ProjectMembers.objects.create(
            team=self.team_c,
            project=self.foreign_project,
            attendee=self.leaver,
            member_role="viewer",
        )

        remove_team_member(self.team_a.team_id, self.leaver.id)

        self.assertTrue(self._in_project(self.leaver, project=self.foreign_project))

    def test_a_grant_from_another_team_survives_leaving_the_host(self):
        """The provenance exception, and the only case that needs one.

        Someone in both teams can hold a folder row that team B's roster
        underwrites while also being a member of host team A. Leaving A
        must not revoke what the grant to B pays for — B's managers
        admitted them and still could.
        """
        TeamMembers.objects.create(team=self.team_b, attendee=self.leaver)
        grant = self.active_folder_grant()
        add_external_participants(grant, [self.leaver.id], self.b_owner)

        remove_team_member(self.team_a.team_id, self.leaver.id)

        row = NoteFolderPermission.objects.get(folder=self.folder, user=self.leaver)
        self.assertEqual(row.via_group_type, "external_grant")
        self.assertIsNotNone(get_folder_role(self.leaver.id, self.folder.folder_id))


class LeftTeamDisappearsFromTheSwitcherTests(CrossTeamTestCase):
    """The user-visible symptom, end to end through the endpoints."""

    def _my_team_ids(self, user):
        self.authenticate(user)
        res = self.client.get(f"/api/v2/team/getMyTeams/?user_id={user.id}")
        self.assertEqual(res.status_code, 200, res.data)
        return {str(team["teamId"]) for team in res.data}

    def test_leaving_removes_the_team_instead_of_demoting_it_to_a_guest_shell(self):
        member = self.a_viewer
        ProjectMembers.objects.create(
            team=self.team_a, project=self.project, attendee=member, member_role="viewer"
        )
        self.assertIn(str(self.team_a.team_id), self._my_team_ids(member))

        self.authenticate(member)
        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.team_a.team_id), "attendee_id": str(member.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        # Before the in-team cascade the surviving `ProjectMembers` row put
        # the team back here as a guest shell, project still open.
        self.assertNotIn(str(self.team_a.team_id), self._my_team_ids(member))

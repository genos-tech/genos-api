"""Deleting an account withdraws the person's cross-team participation (MIP-1550).

A cross-team grant is carried by rows the guest team's managers wrote for the guest team's own
people (a `ProjectMembers` guest row, a `ChannelMember` row, a `NoteFolderPermission` row stamped
`via_group_type="external_grant"`). Nothing re-checks a grant at read time, so a row that outlives
its person is access nobody can see. Removing someone from a team withdraws those rows
(`remove_team_member` -> `drop_team_member_participation`); deleting the ACCOUNT is the other way
a person leaves their teams, and it only flipped `TeamMembers.is_deleted`.

The rules pinned here: the deleted person's own rows on the HOST's objects go; no other person's
row is touched; the grant itself (the two teams' relationship) stays; nobody gains access.
"""

from origin.models.chat.unified_models import ChannelMember
from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.project.prj_models import ProjectMembers
from origin.services.account_deletion import delete_account
from origin.services.external_grants import add_external_participants
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.note_folder_role import get_folder_role


class DeleteAccountWithdrawsExternalParticipationTests(CrossTeamTestCase):
    """`self.b_editor` (a plain member of the GUEST team B) is deleted."""

    def setUp(self):
        super().setUp()
        self.leaver = self.b_editor
        self.stayer = self.b_viewer
        self.project_grant = self.active_project_grant()
        self.folder_grant = self.active_folder_grant()
        self.channel, self.channel_grant = self.shared_chat()
        for grant in (self.project_grant, self.folder_grant, self.channel_grant):
            add_external_participants(grant, [self.leaver.id, self.stayer.id], self.b_owner)

    def _folder_row(self, user):
        return NoteFolderPermission.objects.filter(folder=self.folder, user=user)

    def _channel_row(self, user):
        return ChannelMember.objects.filter(channel=self.channel, user=user, is_deleted=False)

    def test_the_setup_really_admitted_both_people(self):
        for user in (self.leaver, self.stayer):
            self.assertTrue(self._in_project(user))
            self.assertTrue(self._folder_row(user).exists())
            self.assertTrue(self._channel_row(user).exists())

    def test_the_folder_row_goes(self):
        delete_account(self.leaver)
        self.assertFalse(self._folder_row(self.leaver).exists())
        # The row is the access itself, not bookkeeping.
        self.assertIsNone(get_folder_role(self.leaver.id, self.folder.folder_id))

    def test_the_project_guest_row_goes(self):
        delete_account(self.leaver)
        self.assertFalse(self._in_project(self.leaver))

    def test_the_shared_chat_row_goes(self):
        delete_account(self.leaver)
        self.assertFalse(self._channel_row(self.leaver).exists())

    def test_the_other_guest_team_member_keeps_every_row(self):
        delete_account(self.leaver)
        self.assertTrue(self._in_project(self.stayer))
        self.assertTrue(self._folder_row(self.stayer).exists())
        self.assertTrue(self._channel_row(self.stayer).exists())
        self.assertIsNotNone(get_folder_role(self.stayer.id, self.folder.folder_id))

    def test_the_host_teams_own_people_keep_their_rows(self):
        ProjectMembers.objects.get_or_create(
            team=self.team_a,
            project=self.project,
            attendee=self.a_editor,
            defaults={"member_role": "editor"},
        )
        delete_account(self.leaver)
        self.assertTrue(self._in_project(self.a_editor))

    def test_the_grants_themselves_stay_active(self):
        """The relationship is between the teams, not the person: the guest team's managers can
        admit someone else, and the host's connection is not revoked by one departure."""
        delete_account(self.leaver)
        for grant in (self.project_grant, self.folder_grant, self.channel_grant):
            grant.refresh_from_db()
            self.assertEqual(grant.status, ShareStatus.ACTIVE)
        self.assertEqual(ExternalGrant.objects.filter(status=ShareStatus.ACTIVE).count(), 3)

    def test_a_person_in_two_guest_teams_loses_both_shares(self):
        """Each team the person left is walked, not only the first."""
        from origin.models.common.team_models import TeamMembers

        TeamMembers.objects.create(team=self.team_c, attendee=self.leaver)
        delete_account(self.leaver)
        self.assertFalse(self._in_project(self.leaver))
        self.assertFalse(self._folder_row(self.leaver).exists())

    def test_the_summary_says_how_many_rows_were_withdrawn(self):
        summary = delete_account(self.leaver)
        self.assertEqual(summary["external_participation_withdrawn"], 3)

    def test_a_person_with_no_shares_deletes_normally(self):
        summary = delete_account(self.b_owner_free_member())
        self.assertEqual(summary["external_participation_withdrawn"], 0)

    def b_owner_free_member(self):
        from origin.models.common.team_models import TeamMembers
        from origin.services.member_roles import VIEWER

        user = self._user("b_plain")
        TeamMembers.objects.create(team=self.team_b, attendee=user, member_role=VIEWER)
        return user

"""Tests for reporting @-mentions of people who can't reach the surface.

The mention picker offers the whole team, but each surface has its own
narrower membership. These pin the three behaviours that differ, and the
one invariant that matters everywhere: this helper REPORTS, it never
grants.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.mention_membership import (
    SCOPE_CHANNEL,
    SCOPE_PROJECT,
    SCOPE_TEAM_FOLDER,
    non_member_mentions,
)
from origin.views.utils.note_role import ROLE_OWNER


class MentionMembershipTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    # ------------------------------------------------------------------
    # Project scope
    # ------------------------------------------------------------------

    def test_flags_a_user_outside_the_project(self):
        out = non_member_mentions([self.user2.id], project_id=self.project.project_id)
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.user2.id)])
        self.assertEqual(out["scopeKind"], SCOPE_PROJECT)
        self.assertEqual(out["scopeId"], str(self.project.project_id))
        self.assertEqual(out["scopeName"], "Proj")
        self.assertEqual(out["users"][0]["userName"], self.user2.username)

    def test_member_is_not_flagged(self):
        out = non_member_mentions([self.user.id], project_id=self.project.project_id)
        self.assertIsNone(out)

    def test_reporting_does_not_grant(self):
        """The whole point: this is advisory. A flagged user must still
        NOT be a member afterwards."""
        non_member_mentions([self.user2.id], project_id=self.project.project_id)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )

    def test_self_mention_is_excluded(self):
        out = non_member_mentions(
            [self.user2.id],
            project_id=self.project.project_id,
            exclude_user_ids=[self.user2.id],
        )
        self.assertIsNone(out)

    def test_unknown_ids_drop_out_rather_than_surfacing_unlabelled(self):
        import uuid

        out = non_member_mentions([uuid.uuid4()], project_id=self.project.project_id)
        self.assertIsNone(out)

    # ------------------------------------------------------------------
    # Channel scope
    # ------------------------------------------------------------------

    def test_flags_a_user_outside_the_channel(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="GM")
        ChannelMember.objects.create(channel=gm, user=self.user)
        out = non_member_mentions([self.user2.id], channel_id=gm.id)
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.user2.id)])
        self.assertEqual(out["scopeKind"], SCOPE_CHANNEL)
        self.assertEqual(out["scopeName"], "GM")

    def test_removed_channel_member_is_flagged(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="GM")
        ChannelMember.objects.create(channel=gm, user=self.user2, is_deleted=True)
        out = non_member_mentions([self.user2.id], channel_id=gm.id)
        self.assertEqual(len(out["users"]), 1)

    # ------------------------------------------------------------------
    # Team-folder scope
    # ------------------------------------------------------------------

    def _team_folder(self, visibility):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="F",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user, role_id=ROLE_OWNER
        )
        return folder

    def test_private_team_folder_flags_a_non_grantee(self):
        folder = self._team_folder("private")
        out = non_member_mentions([self.user2.id], folder_id=folder.folder_id)
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.user2.id)])
        self.assertEqual(out["scopeKind"], SCOPE_TEAM_FOLDER)

    def test_public_team_folder_flags_nobody(self):
        # Public resolves to Editor for every team member, so there is
        # no one to add.
        folder = self._team_folder("public")
        self.assertIsNone(non_member_mentions([self.user2.id], folder_id=folder.folder_id))

    def test_personal_folder_has_no_membership_to_miss(self):
        """A personal folder grants nothing to anyone — sharing it is the
        per-note dialog's job, so this must stay silent rather than
        offering to 'add' someone to a private sidebar."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Mine", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        self.assertIsNone(non_member_mentions([self.user2.id], folder_id=folder.folder_id))

    def test_no_scope_reports_nothing(self):
        self.assertIsNone(non_member_mentions([self.user2.id]))

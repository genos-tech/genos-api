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
    SCOPE_PERSONAL_NOTE,
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


class PersonalNoteMentionTests(BaseAPITestCase):
    """An @mention in an UNSHARED personal note must not page anyone —
    they'd get a notification for something that 403s, with no recourse.
    Once the note is shared, the recipient is notified normally."""

    def setUp(self):
        super().setUp()
        from origin.models.note.personal_note_models import PersonalNoteMaster

        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Diary", body=[]
        )

    def test_unshared_note_reaches_nobody_but_the_owner(self):
        from origin.views.utils.mention_membership import reachable_mentions

        reach = reachable_mentions(
            [self.user.id, self.user2.id], personal_note_id=self.note.note_id
        )
        self.assertEqual(reach, {str(self.user.id)})

    def test_unshared_note_reports_the_mention_for_sharing(self):
        out = non_member_mentions(
            [self.user2.id],
            personal_note_id=self.note.note_id,
            exclude_user_ids=[self.user.id],
        )
        self.assertEqual(out["scopeKind"], SCOPE_PERSONAL_NOTE)
        self.assertEqual(out["scopeName"], "Diary")
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.user2.id)])

    def test_shared_note_reaches_the_grantee_and_reports_nothing(self):
        from origin.models.note.common_note_models import NotePermissionMaster
        from origin.views.utils.mention_membership import reachable_mentions
        from origin.views.utils.note_role import ROLE_EDITOR

        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=1,
            role_id=ROLE_EDITOR,
        )
        self.assertIn(
            str(self.user2.id),
            reachable_mentions([self.user2.id], personal_note_id=self.note.note_id),
        )
        self.assertIsNone(
            non_member_mentions([self.user2.id], personal_note_id=self.note.note_id)
        )


class TeamNoteMentionNotifyTests(BaseAPITestCase):
    """A mention in a TEAM note must notify everyone mentioned, INCLUDING
    people not yet in the folder.

    Being mentioned into a team note is how you find out it exists; the
    author gets the "add them" prompt to close the access gap. An earlier
    version filtered these by folder reach, so a mention into a private
    folder reached nobody and the recipient never learned of it — that is
    what this guards.
    """

    def setUp(self):
        super().setUp()
        from origin.models.note.personal_note_models import PersonalNoteMaster

        self.folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Secret",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="private",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.folder, user=self.user, role_id=ROLE_OWNER
        )
        self.note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Plan",
            body=[],
            folder_id=self.folder.folder_id,
        )

    def test_team_note_is_not_subject_to_the_personal_suppression(self):
        from origin.views.utils.mention_membership import is_team_folder

        # The view branches on exactly this, so pin it: a team folder
        # must never take the personal-note filtering path.
        self.assertTrue(is_team_folder(self.folder.folder_id))

    def test_private_team_folder_still_reports_the_non_member_for_adding(self):
        out = non_member_mentions(
            [self.user2.id],
            folder_id=self.folder.folder_id,
            exclude_user_ids=[self.user.id],
        )
        self.assertEqual(out["scopeKind"], SCOPE_TEAM_FOLDER)
        self.assertEqual([u["userId"] for u in out["users"]], [str(self.user2.id)])

    def test_personal_note_still_is_suppressed(self):
        """The other side of the branch — the behaviour that WAS wanted."""
        from origin.models.note.personal_note_models import PersonalNoteMaster
        from origin.views.utils.mention_membership import is_team_folder, reachable_mentions

        loose = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Diary", body=[]
        )
        self.assertFalse(is_team_folder(loose.folder_id))
        self.assertEqual(
            reachable_mentions([self.user2.id], personal_note_id=loose.note_id), set()
        )

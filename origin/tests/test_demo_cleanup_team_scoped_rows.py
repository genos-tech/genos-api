"""Regression (MIP-1702): demo teardown must delete rows a demo visitor can create in their team.

`delete_demo_team_data` lists the team-scoped tables by hand. A table it does not list, whose
`team` FK is `SET_NULL`, makes `TeamMaster.delete()` die at COMMIT on

    IntegrityError: update or delete on table "origin_teammaster" violates
    foreign key constraint "origin_mentiongroupm_team_id_..._fk_origin_te"

`delete_demo_environment` then rolls back, `cleanup_demo_users` catches the error per user, and
that user (and their whole workspace) is skipped on every run, forever. The demo seed itself makes
none of these rows, so nothing noticed until the company-scale demo (MIP-1692) planned to add
mention groups. Same shape as the note-version bug (MIP-1514, test_demo_cleanup_note_versions).

MUST be a TransactionTestCase: the FK is DEFERRABLE INITIALLY DEFERRED, so it is only checked at
a real COMMIT, which TestCase never reaches.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NoteFolderTag
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()


class DemoCleanupTeamScopedRowsTests(CheapFlushTransactionTestCase):
    def _seeded_demo_user(self, name):
        """The product's own path: a seeded demo workspace, no hand-built rows."""
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="x", is_demo=True
        )
        info = create_demo_environment(user)
        return user, TeamMaster.objects.get(team_id=info["team_id"])

    def _assert_torn_down(self, user, team, model, **where):
        delete_demo_environment(user)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertFalse(model.objects.filter(**where).exists(), "the row outlived its team")

    def test_a_team_with_a_mention_group_is_deleted(self):
        user, team = self._seeded_demo_user("demo_cl_group")
        group = MentionGroupMaster.objects.create(team=team, group_name="design", created_by=user)
        MentionGroupMembers.objects.create(team=team, group=group, user=user, added_by=user)

        self._assert_torn_down(user, team, MentionGroupMaster, group_id=group.group_id)
        self.assertFalse(MentionGroupMembers.objects.filter(group_id=group.group_id).exists())

    def test_a_team_with_a_note_folder_tag_is_deleted(self):
        user, team = self._seeded_demo_user("demo_cl_tag")
        tag = NoteFolderTag.objects.create(team=team, name="Runbooks", created_by=user)

        self._assert_torn_down(user, team, NoteFolderTag, tag_id=tag.tag_id)

    def test_a_team_with_a_pending_invite_is_deleted(self):
        user, team = self._seeded_demo_user("demo_cl_invite")
        invite = TeamInvite.objects.create(
            team=team,
            invited_email="friend@example.com",
            invited_by=user,
            token_hash="0" * 64,
            expires_at=timezone.now() + timedelta(days=7),
        )

        self._assert_torn_down(user, team, TeamInvite, id=invite.id)

    def test_a_team_with_a_custom_emoji_is_deleted(self):
        user, team = self._seeded_demo_user("demo_cl_emoji")
        emoji = TeamEmojiMaster.objects.create(
            team=team,
            name="party",
            image=SimpleUploadedFile("party.png", b"\x89PNG\r\n\x1a\n", content_type="image/png"),
            created_by=user,
        )

        self._assert_torn_down(user, team, TeamEmojiMaster, emoji_id=emoji.emoji_id)

    def test_another_teams_rows_are_left_alone(self):
        """The sweep is scoped to the one team: a second demo team keeps its own rows."""
        gone_user, gone_team = self._seeded_demo_user("demo_cl_gone")
        kept_user, kept_team = self._seeded_demo_user("demo_cl_kept")
        MentionGroupMaster.objects.create(team=gone_team, group_name="design", created_by=gone_user)
        kept_group = MentionGroupMaster.objects.create(
            team=kept_team, group_name="design", created_by=kept_user
        )
        kept_tag = NoteFolderTag.objects.create(
            team=kept_team, name="Runbooks", created_by=kept_user
        )

        delete_demo_environment(gone_user)

        self.assertFalse(TeamMaster.objects.filter(team_id=gone_team.team_id).exists())
        self.assertEqual(
            MentionGroupMaster.objects.get(group_id=kept_group.group_id).team_id, kept_team.team_id
        )
        self.assertEqual(
            NoteFolderTag.objects.get(tag_id=kept_tag.tag_id).team_id, kept_team.team_id
        )

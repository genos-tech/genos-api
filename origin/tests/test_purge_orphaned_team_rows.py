"""MIP-1704: `purge_orphaned_team_rows` removes what earlier demo teardowns left with team = NULL.

The mechanism is the real one wherever it can be: an emoji's image path comes from the model's own
`_team_emoji_path`, and an orphan is made the way the database made it, by nulling `team` on a
row that had one (SET_NULL), not by hand-writing a path.
"""

import shutil
import tempfile
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NoteFolderTag
from origin.services.orphaned_team_rows import purge_orphaned_team_rows

User = get_user_model()

PNG = b"\x89PNG\r\n\x1a\n"


class PurgeOrphanedTeamRowsTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="mip1704-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(
            username="owner1704", email="owner1704@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Live team", team_email="live1704@example.com", owner=self.user
        )
        self.gone = TeamMaster.objects.create(
            team_name="Gone team", team_email="gone1704@example.com", owner=self.user
        )

    # --- helpers -------------------------------------------------------------------------

    def _emoji(self, team, name, created_by=None):
        return TeamEmojiMaster.objects.create(
            team=team,
            name=name,
            image=SimpleUploadedFile(f"{name}.png", PNG, content_type="image/png"),
            created_by=created_by,
        )

    def _orphan(self, row):
        """What deleting the team did to the row: SET_NULL, nothing else."""
        type(row).objects.filter(pk=row.pk).update(team=None)
        row.refresh_from_db()
        return row

    def _global_default(self, name):
        """Exactly what `seed_default_emoji` writes: no team, no creator, path scope `global`."""
        emoji = TeamEmojiMaster(team=None, name=name, created_by=None)
        emoji.image_ext = "png"
        emoji.image.save(f"{name}.png", ContentFile(PNG), save=True)
        return emoji

    def _run(self, *args):
        out = StringIO()
        call_command("purge_orphaned_team_rows", *args, stdout=out)
        return out.getvalue()

    # --- what is an orphan ---------------------------------------------------------------

    def test_a_mention_group_without_a_team_goes_with_its_members(self):
        group = MentionGroupMaster.objects.create(
            team=self.gone, group_name="design", created_by=self.user
        )
        MentionGroupMembers.objects.create(
            team=self.gone, group=group, user=self.user, added_by=self.user
        )
        self._orphan(group)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.mention_groups, 1)
        self.assertFalse(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())
        self.assertFalse(MentionGroupMembers.objects.filter(group_id=group.group_id).exists())

    def test_a_note_folder_tag_without_a_team_goes(self):
        tag = self._orphan(NoteFolderTag.objects.create(team=self.gone, name="Runbooks"))

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.note_folder_tags, 1)
        self.assertFalse(NoteFolderTag.objects.filter(tag_id=tag.tag_id).exists())

    def test_an_invite_without_a_team_goes(self):
        invite = TeamInvite.objects.create(
            team=self.gone,
            invited_email="friend@example.com",
            invited_by=self.user,
            token_hash="0" * 64,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self._orphan(invite)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.invites, 1)
        self.assertFalse(TeamInvite.objects.filter(id=invite.id).exists())

    def test_rows_of_a_live_team_are_never_touched(self):
        group = MentionGroupMaster.objects.create(
            team=self.team, group_name="design", created_by=self.user
        )
        tag = NoteFolderTag.objects.create(team=self.team, name="Runbooks")
        invite = TeamInvite.objects.create(
            team=self.team,
            invited_email="friend@example.com",
            invited_by=self.user,
            token_hash="1" * 64,
            expires_at=timezone.now() + timedelta(days=7),
        )
        emoji = self._emoji(self.team, "party", created_by=self.user)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.total, 0)
        self.assertTrue(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())
        self.assertTrue(NoteFolderTag.objects.filter(tag_id=tag.tag_id).exists())
        self.assertTrue(TeamInvite.objects.filter(id=invite.id).exists())
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())
        self.assertTrue(default_storage.exists(emoji.image.name))

    # --- the one that could do damage: the global default emojis -------------------------

    def test_a_global_default_emoji_is_never_an_orphan(self):
        """team = NULL, no creator, on purpose. Deleting it would blank every team's catalog."""
        default = self._global_default("party-blob")
        self.assertIn("/global/", default.image.name)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.custom_emojis, 0)
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=default.emoji_id).exists())
        self.assertTrue(default_storage.exists(default.image.name))

    def test_an_orphaned_custom_emoji_goes_with_its_image_file(self):
        emoji = self._orphan(self._emoji(self.gone, "party", created_by=self.user))
        path = emoji.image.name
        self.assertIn(str(self.gone.team_id), path)  # the old team's uuid is in the path
        self.assertTrue(default_storage.exists(path))

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.custom_emojis, 1)
        self.assertEqual(report.emoji_files_removed, 1)
        self.assertFalse(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())
        self.assertFalse(default_storage.exists(path))

    def test_an_orphan_whose_creator_is_gone_too_is_still_told_from_a_default(self):
        """Demo teardown deletes the user as well, so `created_by` is NULL on the orphan: the very
        thing a default has. Only the path tells them apart."""
        orphan = self._emoji(self.gone, "orphan", created_by=None)
        self._orphan(orphan)
        default = self._global_default("default-one")

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.custom_emojis, 1)
        self.assertFalse(TeamEmojiMaster.objects.filter(emoji_id=orphan.emoji_id).exists())
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=default.emoji_id).exists())

    def test_a_soft_deleted_orphan_goes_too(self):
        emoji = self._emoji(self.gone, "old", created_by=self.user)
        TeamEmojiMaster.objects.filter(pk=emoji.pk).update(is_deleted=True)
        self._orphan(emoji)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.custom_emojis, 1)
        self.assertFalse(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())

    def test_a_team_less_emoji_with_an_unrecognised_path_is_left_alone(self):
        """Not provably an orphan (a legacy default, say): kept. The safe direction."""
        legacy = TeamEmojiMaster.objects.create(
            team=None,
            name="legacy",
            image="slackmoji/legacy.png",
            created_by=None,
        )

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.custom_emojis, 0)
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=legacy.emoji_id).exists())

    def test_an_emoji_whose_file_cannot_be_deleted_is_kept_for_the_next_run(self):
        emoji = self._orphan(self._emoji(self.gone, "stuck", created_by=self.user))

        from unittest import mock

        with mock.patch(
            "django.db.models.fields.files.FieldFile.delete", side_effect=OSError("storage down")
        ):
            report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.emoji_file_errors, 1)
        self.assertEqual(report.custom_emojis, 0)
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())
        # Next run, storage back: it goes.
        again = purge_orphaned_team_rows(apply=True)
        self.assertEqual(again.custom_emojis, 1)
        self.assertFalse(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())

    # --- the command ---------------------------------------------------------------------

    def _one_of_each(self):
        group = MentionGroupMaster.objects.create(
            team=self.gone, group_name="design", created_by=self.user
        )
        tag = NoteFolderTag.objects.create(team=self.gone, name="Runbooks")
        invite = TeamInvite.objects.create(
            team=self.gone,
            invited_email="friend@example.com",
            invited_by=self.user,
            token_hash="2" * 64,
            expires_at=timezone.now() + timedelta(days=7),
        )
        emoji = self._emoji(self.gone, "party", created_by=self.user)
        for row in (group, tag, invite, emoji):
            self._orphan(row)
        return group, tag, invite, emoji

    def test_a_dry_run_counts_and_writes_nothing(self):
        group, tag, invite, emoji = self._one_of_each()

        out = self._run()

        self.assertIn("1 mention group(s) would be deleted", out)
        self.assertIn("1 note folder tag(s) would be deleted", out)
        self.assertIn("1 invite(s) would be deleted", out)
        self.assertIn("1 custom emoji(s) would be deleted", out)
        self.assertIn("dry run", out)
        self.assertTrue(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())
        self.assertTrue(NoteFolderTag.objects.filter(tag_id=tag.tag_id).exists())
        self.assertTrue(TeamInvite.objects.filter(id=invite.id).exists())
        self.assertTrue(TeamEmojiMaster.objects.filter(emoji_id=emoji.emoji_id).exists())
        self.assertTrue(default_storage.exists(emoji.image.name))

    def test_apply_needs_yes(self):
        group, *_ = self._one_of_each()

        with self.assertRaises(CommandError):
            self._run("--apply")

        self.assertTrue(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())

    def test_apply_deletes_and_a_second_run_finds_nothing(self):
        self._one_of_each()

        out = self._run("--apply", "--yes")
        self.assertIn("1 mention group(s) deleted", out)
        self.assertIn("1 emoji image file(s) removed", out)

        again = self._run("--apply", "--yes")
        self.assertIn("0 mention group(s) deleted", again)
        self.assertIn("0 custom emoji(s) deleted", again)

    # --- the survey: counted, never deleted ---------------------------------------------

    def test_other_tables_with_a_null_team_are_reported_but_not_deleted(self):
        """A row in a table this command does not purge (here a group MEMBER whose team went null
        while its group is alive) is shown to a human and left alone."""
        group = MentionGroupMaster.objects.create(
            team=self.team, group_name="design", created_by=self.user
        )
        member = MentionGroupMembers.objects.create(
            team=self.team, group=group, user=self.user, added_by=self.user
        )
        MentionGroupMembers.objects.filter(pk=member.pk).update(team=None)

        out = self._run("--apply", "--yes")

        self.assertIn("origin.MentionGroupMembers.team: 1", out)
        self.assertIn("NOT deleted", out)
        self.assertTrue(MentionGroupMembers.objects.filter(pk=member.pk).exists())
        self.assertTrue(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())

    def test_the_survey_does_not_list_a_table_the_command_purges(self):
        orphan = self._orphan(
            MentionGroupMaster.objects.create(
                team=self.gone, group_name="design", created_by=self.user
            )
        )

        report = purge_orphaned_team_rows(apply=False)

        self.assertEqual(report.mention_groups, 1)
        self.assertNotIn("origin.MentionGroupMaster.team", report.survey)
        self.assertTrue(MentionGroupMaster.objects.filter(group_id=orphan.group_id).exists())

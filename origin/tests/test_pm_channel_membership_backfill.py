"""Tests for `backfill_pm_channel_membership`.

The management command reconciles PM channels + their `ChannelMember`
rows to project membership — fixing rows that predate
`pm_channel_signals` (which only maintain the invariant going forward).

Because those signals fire on normal `.create()`, the test setup already
produces a correct channel + member; we then simulate the historical gap
(delete the auto-created rows / soft-delete a member / bulk_create a
signal-bypassing row) and assert the command repairs it.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

User = get_user_model()


class BackfillPmChannelMembershipTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bf-user", email="bf@test.com", password="pass12345"
        )
        self.team = TeamMaster.objects.create(
            team_name="BF Team", team_email="bfteam@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="BF Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # Signal creates the PM channel on project save.
        self.channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        # Signal creates the ChannelMember on membership save.
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user
        )

    def _active_member_exists(self) -> bool:
        return ChannelMember.objects.filter(
            channel=self.channel, user_id=self.user.id, is_deleted=False
        ).exists()

    def test_recreates_missing_channel_member(self):
        # Simulate a membership created before the signal: the PM channel
        # exists but the ChannelMember row was never made.
        ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).delete()
        self.assertFalse(self._active_member_exists())

        call_command("backfill_pm_channel_membership")

        self.assertTrue(self._active_member_exists())
        self.assertEqual(
            ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).count(),
            1,
        )

    def test_is_idempotent(self):
        ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).delete()
        call_command("backfill_pm_channel_membership")
        call_command("backfill_pm_channel_membership")
        self.assertEqual(
            ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).count(),
            1,
        )

    def test_reactivates_soft_deleted_member(self):
        # A live ProjectMembers row + a soft-deleted ChannelMember is the
        # exact inconsistency to repair (projects have no soft-delete).
        ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).update(
            is_deleted=True
        )
        self.assertFalse(self._active_member_exists())

        call_command("backfill_pm_channel_membership")

        self.assertTrue(self._active_member_exists())

    def test_dry_run_writes_nothing(self):
        ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).delete()
        call_command("backfill_pm_channel_membership", "--dry-run")
        self.assertFalse(self._active_member_exists())

    def test_recreates_missing_pm_channel_and_member(self):
        # Whole PM channel missing (project predates the channel signal).
        ChannelMember.objects.filter(channel=self.channel).delete()
        self.channel.delete()
        self.assertFalse(
            Channel.objects.filter(
                project_id=self.project.project_id, kind=ChannelKind.PM
            ).exists()
        )

        call_command("backfill_pm_channel_membership")

        channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        self.assertFalse(channel.is_deleted)
        self.assertEqual(channel.legacy_chat_id, self.project.project_id)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=channel, user_id=self.user.id, is_deleted=False
            ).exists()
        )

    def test_skips_null_attendee_rows(self):
        ChannelMember.objects.filter(channel=self.channel, user_id=self.user.id).delete()
        # bulk_create bypasses the post_save signal — mirrors the corrupt
        # null-attendee seed rows seen in production.
        ProjectMembers.objects.bulk_create(
            [ProjectMembers(team=self.team, project=self.project, attendee=None)]
        )

        call_command("backfill_pm_channel_membership")

        # Real member repaired; null-attendee row produced no ChannelMember.
        self.assertTrue(self._active_member_exists())
        self.assertEqual(
            ChannelMember.objects.filter(channel=self.channel).count(), 1
        )

    def test_skips_deleted_project(self):
        deleted_user = User.objects.create_user(
            username="bf-del", email="bfdel@test.com", password="pass12345"
        )
        deleted_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Deleted Project",
            owner=deleted_user,
            project_system_user=deleted_user,
            is_deleted=True,
        )
        # Remove the channel the signal may have created so we can assert
        # the backfill does NOT resurrect one for a deleted project.
        Channel.objects.filter(
            project_id=deleted_project.project_id, kind=ChannelKind.PM
        ).delete()
        ProjectMembers.objects.create(
            team=self.team, project=deleted_project, attendee=deleted_user
        )

        call_command("backfill_pm_channel_membership")

        self.assertFalse(
            Channel.objects.filter(
                project_id=deleted_project.project_id, kind=ChannelKind.PM
            ).exists()
        )

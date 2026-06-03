"""Tests for `backfill_pm_channel_avatar`.

The command reconciles each PM channel's `profile_image_url` to its
project's stored avatar (`ProjectMaster.profile_image_file_name`) — fixing
projects whose avatar was uploaded before `_ensure_pm_channel_for_project`
mirrored the image (it only fires on a project save).

Setup note: the signal fires on normal `.create()`/`.save()`, so to
reproduce the historical gap we set the project's image via a queryset
`.update()` (which does NOT emit post_save), leaving the channel stale.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster

User = get_user_model()

_IMG = "project_profiles/1/profile.jpg?v=123"


class BackfillPmChannelAvatarTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bf-av", email="bfav@test.com", password="pass12345"
        )
        self.team = TeamMaster.objects.create(
            team_name="BF Av Team", team_email="bfav@team.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="BF Avatar Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # Signal creates the PM channel on project save — with no avatar.
        self.channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        self.assertEqual(self.channel.profile_image_url, "")

    def _set_project_image_bypassing_signal(self, image: str) -> None:
        # `.update()` skips post_save, so the channel stays stale — exactly
        # the pre-mirror data state the backfill must repair.
        ProjectMaster.objects.filter(pk=self.project.pk).update(
            profile_image_file_name=image
        )

    def _channel_image(self) -> str:
        return Channel.objects.get(pk=self.channel.pk).profile_image_url

    def test_backfills_stale_channel_avatar(self):
        self._set_project_image_bypassing_signal(_IMG)
        self.assertEqual(self._channel_image(), "")  # gap reproduced

        call_command("backfill_pm_channel_avatar")

        self.assertEqual(self._channel_image(), _IMG)

    def test_updates_drifted_avatar(self):
        # Channel carries an old image; project moved to a new one.
        Channel.objects.filter(pk=self.channel.pk).update(
            profile_image_url="project_profiles/1/old.jpg"
        )
        self._set_project_image_bypassing_signal(_IMG)

        call_command("backfill_pm_channel_avatar")

        self.assertEqual(self._channel_image(), _IMG)

    def test_is_idempotent(self):
        self._set_project_image_bypassing_signal(_IMG)
        call_command("backfill_pm_channel_avatar")
        call_command("backfill_pm_channel_avatar")
        self.assertEqual(self._channel_image(), _IMG)

    def test_dry_run_writes_nothing(self):
        self._set_project_image_bypassing_signal(_IMG)
        call_command("backfill_pm_channel_avatar", "--dry-run")
        self.assertEqual(self._channel_image(), "")

    def test_skips_project_without_image(self):
        # No project image → channel left empty, no error.
        call_command("backfill_pm_channel_avatar")
        self.assertEqual(self._channel_image(), "")

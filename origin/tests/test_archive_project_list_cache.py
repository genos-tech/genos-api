"""Archive / restore clear the project-list cache, both variants (MIP-1886)."""

from __future__ import annotations

from django.core.cache import cache
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase

PROJECTS = "/api/v2/project/projects/"
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class ArchiveProjectListCacheTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate(self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Launch",
            code="LAU",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _list(self, **extra):
        res = self.client.get(PROJECTS, {"team_id": self.team.team_id, **extra})
        self.assertEqual(res.status_code, 200, res.data)
        return str(res.data)

    def _toggle(self, url):
        res = self.client.post(
            f"{url}?team_id={self.team.team_id}&project_id={self.project.project_id}",
            {},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_archive_removes_it_from_a_warm_list_and_restore_brings_it_back(self):
        self.assertIn("Launch", self._list())  # warms the cache
        self._toggle("/api/v2/project/archive/")
        self.assertNotIn("Launch", self._list())
        self._toggle("/api/v2/project/unarchive/")
        self.assertIn("Launch", self._list())

    def test_archive_moves_ts_updated_at_for_delta_sync(self):
        before = ProjectMaster.objects.get(pk=self.project.pk).ts_updated_at
        self._toggle("/api/v2/project/archive/")
        self.assertGreater(ProjectMaster.objects.get(pk=self.project.pk).ts_updated_at, before)

    def test_include_archived_list_is_fresh_after_a_create(self):
        self._toggle("/api/v2/project/archive/")
        self.assertIn("Launch", self._list(include_archived="true"))  # warms ":archived"
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Fresh one",
            code="FRE",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        self.assertIn("Fresh one", self._list(include_archived="true"))

    def test_group_chat_archive_moves_ts_updated_at(self):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Crew", owner=self.user, is_private=True
        )
        ChannelMember.objects.create(channel=channel, user=self.user)
        before = Channel.objects.get(pk=channel.pk).ts_updated_at
        from origin.services.archive import archive_group_chat

        archive_group_chat(channel, actor=self.user)
        after = Channel.objects.get(pk=channel.pk)
        self.assertIsNotNone(after.archived_at)
        self.assertGreater(after.ts_updated_at, before)

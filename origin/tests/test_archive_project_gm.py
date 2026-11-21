"""Archiving a project and a group chat instead of deleting it (MIP-1693).

An archive is a reversible state: the owner sets `archived_at`, the item leaves the default lists,
and every write path refuses with the same 409 until the owner restores it. Nothing is deleted or
moved: tasks, notes, files, history and chat stay as they are.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services.archive import archived_project_response
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

ARCHIVE = "/api/v2/project/archive/"
RESTORE = "/api/v2/project/unarchive/"
PROJECTS = "/api/v2/project/projects/"


class ArchiveProjectTests(BaseAPITestCase):
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
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            project=self.project,
            team=self.team,
            title="Existing task",
            reporter=self.user,
        )

    def _post(self, url):
        return self.client.post(
            f"{url}?team_id={self.team.team_id}&project_id={self.project.project_id}",
            {},
            format="json",
        )

    def test_owner_archives_and_restores(self):
        res = self._post(ARCHIVE)
        self.assertEqual(res.status_code, 200, res.data)
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.archived_at)
        # Everything is kept.
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())

        res = self._post(RESTORE)
        self.assertEqual(res.status_code, 200, res.data)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.archived_at)

    def test_non_owner_cannot_archive(self):
        self.authenticate(self.user2)
        res = self._post(ARCHIVE)
        self.assertEqual(res.status_code, 403, res.data)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.archived_at)

    def test_archived_project_leaves_default_list_and_shows_with_flag(self):
        self._post(ARCHIVE)
        res = self.client.get(PROJECTS, {"team_id": self.team.team_id})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("Launch", str(res.data))

        res = self.client.get(PROJECTS, {"team_id": self.team.team_id, "include_archived": "true"})
        self.assertIn("Launch", str(res.data))

    def test_archived_project_rejects_task_create(self):
        self._post(ARCHIVE)
        res = self.client.post(
            "/api/v2/task/",
            {"project": self.project.project_id, "title": "New task"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(TaskMaster.objects.filter(title="New task").exists())

    def test_archived_project_rejects_task_edit(self):
        self._post(ARCHIVE)
        res = self.client.put(
            "/api/v2/task/",
            {"project": self.project.project_id, "task_id": self.task.task_id, "title": "Renamed"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Existing task")

    def test_archived_project_rejects_comment(self):
        self._post(ARCHIVE)
        res = self.client.post(
            "/api/v2/task/comment/",
            {"task_id": self.task.task_id, "comment": "hello"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)

    def test_restore_brings_writes_back(self):
        self._post(ARCHIVE)
        self._post(RESTORE)
        res = self.client.post(
            "/api/v2/task/",
            {
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "title": "After restore",
                "assignee": str(self.user.id),
                "reporter": str(self.user.id),
                "priority": "Normal",
                "effort_level": "Moderate",
                "status": "Open",
                "content": None,
                "due_date": "2026-12-31",
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(TaskMaster.objects.filter(title="After restore").exists())

    def test_archive_and_restore_write_counts_only_security_rows(self):
        self._post(ARCHIVE)
        self._post(RESTORE)
        events = list(
            SecurityEvent.objects.filter(
                action__in=[SecurityAction.PROJECT_ARCHIVED, SecurityAction.PROJECT_RESTORED]
            ).order_by("ts_created_at")
        )
        self.assertEqual([e.action for e in events], ["project.archived", "project.restored"])
        for event in events:
            self.assertEqual(event.team_id, self.team.team_id)
            # Counts and ids only: no names in the metadata.
            self.assertNotIn("Launch", str(event.metadata))
            self.assertIn("tasks", event.metadata)

    def test_guard_is_none_for_a_live_project(self):
        self.assertIsNone(archived_project_response(self.project.project_id))


class ArchiveGroupChatTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.gm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Ops",
            owner=self.user,
            is_private=True,
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user)
        ChannelMember.objects.create(channel=self.gm, user=self.user2)

    def test_owner_archives_and_archived_gm_rejects_messages(self):
        res = self.client.post(f"/api/v3/channels/{self.gm.id}/archive/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.gm.refresh_from_db()
        self.assertIsNotNone(self.gm.archived_at)

        res = self.client.post(
            f"/api/v3/channels/{self.gm.id}/messages/",
            {"body": [{"type": "paragraph", "content": []}], "body_text": "hi"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)

    def test_restore_gm_accepts_messages_again(self):
        self.client.post(f"/api/v3/channels/{self.gm.id}/archive/", {}, format="json")
        res = self.client.post(f"/api/v3/channels/{self.gm.id}/unarchive/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.gm.refresh_from_db()
        self.assertIsNone(self.gm.archived_at)

    def test_archived_gm_leaves_chat_list_until_asked(self):
        self.client.post(f"/api/v3/channels/{self.gm.id}/archive/", {}, format="json")
        res = self.client.get("/api/v3/channels/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(str(self.gm.id), str(res.data))
        res = self.client.get(
            "/api/v3/channels/",
            {"team_id": str(self.team.team_id), "include_archived": "true"},
        )
        self.assertIn(str(self.gm.id), str(res.data))

    def test_non_owner_cannot_archive_gm(self):
        self.authenticate(self.user2)
        res = self.client.post(f"/api/v3/channels/{self.gm.id}/archive/", {}, format="json")
        self.assertEqual(res.status_code, 403, res.data)
        self.gm.refresh_from_db()
        self.assertIsNone(self.gm.archived_at)

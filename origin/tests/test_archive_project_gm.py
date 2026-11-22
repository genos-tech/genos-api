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
from origin.models.project.workspace_change_models import WorkspaceChange
from origin.models.task.task_models import TaskMaster
from origin.serializers.project.prj_serializers import ProjectMasterSerializer
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

    def test_archive_and_restore_write_workspace_change_rows(self):
        base = f"/api/v3/channels/{self.gm.id}"
        self.client.post(f"{base}/archive/", {}, format="json")
        self.client.post(f"{base}/archive/", {}, format="json")  # already archived: no new row
        self.client.post(f"{base}/unarchive/", {}, format="json")
        rows = list(
            WorkspaceChange.objects.filter(entity_kind="channel", entity_uuid=self.gm.id).order_by(
                "change_id"
            )
        )
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual((r.action, r.fields, r.entity_id), ("update", ["archived_at"], None))
            self.assertEqual(r.team_id, self.team.team_id)

        res = self.client.get(f"{base}/changes/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(len(res.data["changes"]), 2)
        self.assertEqual(res.data["changes"][0]["entity_id"], str(self.gm.id))

    def test_changes_hidden_from_a_non_member(self):
        stranger = User.objects.create_user(
            username="stranger", email="s@example.com", password="x"
        )
        self.authenticate(stranger)
        res = self.client.get(f"/api/v3/channels/{self.gm.id}/changes/")
        self.assertEqual(res.status_code, 404)

    def test_account_export_lists_the_channel_row(self):
        from origin.services.account_export import build_export

        self.client.post(f"/api/v3/channels/{self.gm.id}/archive/", {}, format="json")
        data = build_export(self.user)
        text = str(data)
        self.assertIn(str(self.gm.id), text)

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


class ArchivedFlagOnRowsTests(BaseAPITestCase):
    """The list and detail rows say whether they are archived, so the client needs no second
    request to find out (MIP-1853). `archivedAt` is the time the owner archived them, null when live.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate(self.user)
        self.live = ProjectMaster.objects.create(
            team=self.team,
            project_name="Live",
            code="LIV",
            owner=self.user,
            project_system_user=self.user,
        )
        self.old = ProjectMaster.objects.create(
            team=self.team,
            project_name="Old",
            code="OLD",
            owner=self.user,
            project_system_user=self.user,
        )
        for project in (self.live, self.old):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.client.post(
            f"{ARCHIVE}?team_id={self.team.team_id}&project_id={self.old.project_id}",
            {},
            format="json",
        )
        self.gm_live = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Open", owner=self.user, is_private=True
        )
        self.gm_archived = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Shut", owner=self.user, is_private=True
        )
        for gm in (self.gm_live, self.gm_archived):
            ChannelMember.objects.create(channel=gm, user=self.user)
        self.client.post(f"/api/v3/channels/{self.gm_archived.id}/archive/", {}, format="json")

    def _row(self, rows, key, value):
        matches = [row for row in rows if row[key] == value]
        self.assertEqual(len(matches), 1, rows)
        return matches[0]

    def test_project_rows_carry_archived_and_archived_at(self):
        res = self.client.get(PROJECTS, {"team_id": self.team.team_id, "include_archived": "true"})
        self.assertEqual(res.status_code, 200, res.data)
        live = self._row(res.data, "projectId", self.live.project_id)
        old = self._row(res.data, "projectId", self.old.project_id)
        self.assertIs(live["archived"], False)
        self.assertIsNone(live["archivedAt"])
        self.assertIs(old["archived"], True)
        self.assertIsNotNone(old["archivedAt"])

    def test_project_detail_row_carries_archived(self):
        # The detail row is the serializer the PUT (update) and the create responses return.
        self.old.refresh_from_db()
        self.live.refresh_from_db()
        old = ProjectMasterSerializer(self.old).data
        live = ProjectMasterSerializer(self.live).data
        self.assertIs(old["archived"], True)
        self.assertIsNotNone(old["archived_at"])
        self.assertIs(live["archived"], False)
        self.assertIsNone(live["archived_at"])

    def test_chat_rows_carry_archived_and_archived_at(self):
        res = self.client.get(
            "/api/v3/channels/",
            {"team_id": str(self.team.team_id), "include_archived": "true"},
        )
        self.assertEqual(res.status_code, 200, res.data)
        rows = res.data["channels"]
        shut = self._row(rows, "id", str(self.gm_archived.id))
        open_ = self._row(rows, "id", str(self.gm_live.id))
        self.assertIs(shut["archived"], True)
        self.assertIsNotNone(shut["archivedAt"])
        self.assertIs(open_["archived"], False)
        self.assertIsNone(open_["archivedAt"])

    def test_chat_detail_row_carries_archived(self):
        res = self.client.get(f"/api/v3/channels/{self.gm_archived.id}/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIs(res.data["channel"]["archived"], True)

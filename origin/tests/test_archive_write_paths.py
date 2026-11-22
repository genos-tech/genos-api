"""An archived project / group chat is read-only on every write path (MIP-1887).

REST views, services (sprints, tags, labels, rename) and the MCP gate, which writes through the
agent registry and never touches the REST views.
"""

from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.archive import ARCHIVED_MESSAGE, archived_refusal_for_args
from origin.services.project_catalog import CatalogRefused, add_project_tag
from origin.services.project_manage import ProjectRefused, set_project_name
from origin.services.sprint_manage import SprintRefused, create_sprint
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import McpBase


class ArchivedRestWritesTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate(self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Old",
            code="OLD",
            owner=self.user,
            project_system_user=self.user,
            archived_at=timezone.now(),
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            project=self.project, team=self.team, title="Existing", reporter=self.user
        )

    def test_milestone_and_folder_create_are_refused(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "M1"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertFalse(MilestoneMaster.objects.filter(title="M1").exists())
        res = self.client.post(
            "/api/v2/milestone/folder/",
            {"project_id": self.project.project_id, "name": "F"},
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)

    def test_task_delete_is_refused(self):
        res = self.client.delete(
            "/api/v2/task/",
            {"team_id": str(self.team.team_id), "task_id": self.task.task_id},
        )
        res = self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id={self.task.task_id}"
            "&is_init_task_boolean=0"
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())

    def test_services_refuse_with_their_own_error(self):
        with self.assertRaises(SprintRefused) as sp:
            create_sprint(
                project=self.project,
                name="S",
                start=timezone.now().date(),
                end=timezone.now().date(),
            )
        self.assertEqual((sp.exception.message, sp.exception.status), (ARCHIVED_MESSAGE, 409))
        with self.assertRaises(CatalogRefused):
            add_project_tag(self.project, "bug")
        with self.assertRaises(ProjectRefused):
            set_project_name(self.project, "Renamed", self.user.id)

    def test_the_target_scan_finds_task_project_and_nested_items(self):
        self.assertEqual(
            archived_refusal_for_args({"task_id": self.task.task_id}), ARCHIVED_MESSAGE
        )
        self.assertEqual(
            archived_refusal_for_args({"items": [{"project_id": self.project.project_id}]}),
            ARCHIVED_MESSAGE,
        )
        self.assertIsNone(archived_refusal_for_args({"title": "x", "task_id": "abc"}))


class ArchivedChatWritesTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Ops", owner=self.user, is_private=True
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user)
        self.msg = Message.objects.create(
            channel=self.gm,
            sender=self.user,
            seq=1,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}],
            body_text="hi",
        )
        self.gm.archived_at = timezone.now()
        self.gm.save(update_fields=["archived_at"])

    def test_edit_delete_and_react_are_refused(self):
        url = f"/api/v3/messages/{self.msg.id}/"
        res = self.client.patch(
            url, {"body": [{"type": "paragraph"}], "body_text": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(self.client.delete(url).status_code, 409)
        res = self.client.post(f"{url}reactions/", {"emoji": "👍"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, "hi")
        self.assertIsNone(self.msg.deleted_at)

    def test_group_rename_is_refused(self):
        res = self.client.patch(f"/api/v3/channels/{self.gm.id}/", {"title": "New"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)


class ArchivedMcpWritesTests(McpBase):
    def setUp(self):
        super().setUp()
        self.project.archived_at = timezone.now()
        self.project.save(update_fields=["archived_at"])
        self._key()

    def _text(self, res):
        result = self._result(res)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def test_create_task_and_add_comment_are_refused(self):
        text = self._text(
            self._call("create_task", {"title": "New", "project_id": self.project.project_id})
        )
        self.assertIn("archived", text)
        self.assertFalse(TaskMaster.objects.filter(title="New").exists())
        text = self._text(self._call("add_comment", {"task_id": "WRD-7", "body_text": "hello"}))
        self.assertIn("archived", text)
        self.assertEqual(TaskComments.objects.filter(task=self.task).count(), 0)

    def test_reads_and_live_projects_still_work(self):
        self._result(self._call("get_task", {"task_id": "WRD-7"}))
        live = ProjectMaster.objects.create(
            team=self.team,
            project_name="Live",
            code="LIV",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=live, attendee=self.user)
        res = self._result(
            self._call("create_task", {"title": "Ok", "project_id": live.project_id})
        )
        self.assertFalse(res.get("isError"), res)

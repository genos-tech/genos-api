"""An archived project / group chat is read-only on every write path (MIP-1887).

REST views, services (sprints, tags, labels, rename) and the MCP gate, which writes through the
agent registry and never touches the REST views.
"""

from __future__ import annotations

import io

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.services.archive import (
    ARCHIVED_MESSAGE,
    archived_refusal_for_args,
    archived_task_response,
)
from origin.services.project_catalog import CatalogRefused, add_project_tag
from origin.services.project_manage import ProjectRefused, set_project_name
from origin.services.sprint_manage import SprintRefused, create_sprint
from origin.tests.test_api_contract import ContractTestBase
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


class ArchivedRestPathsTests(ContractTestBase):
    """MIP-2578: the paths that only refused View only now refuse an archived project too."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.team_id = str(self.team.team_id)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Old",
            code="OLD2",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.pid = self.project.project_id
        self.task, self.other = (
            TaskMaster.objects.create(
                project=self.project, team=self.team, title=t, status="Open", reporter=self.user
            )
            for t in ("One", "Two")
        )
        self.third = TaskMaster.objects.create(
            project=self.project, team=self.team, title="Three", status="Open", reporter=self.user
        )
        self.dependency = TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.other, team=self.team
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", task=self.third, reporter=self.user
        )
        ProjectMaster.objects.filter(pk=self.project.pk).update(archived_at=timezone.now())

    def assert_archived(self, response):
        self.assertEqual(response.status_code, 409, getattr(response, "data", None))
        self.assertEqual(response.data["code"], "archived")

    def post(self, url, data, **kw):
        return self.client.post(url, data, format=kw.pop("format", "json"))

    def test_public_api(self):
        self.assert_archived(
            self.post(
                f"/api/public/v1/tasks/?{self.team_q}", {"project_id": self.pid, "title": "New"}
            )
        )
        self.assert_archived(
            self.client.patch(
                f"/api/public/v1/tasks/{self.task.task_id}/", {"title": "Edited"}, format="json"
            )
        )
        self.assertEqual(TaskMaster.objects.filter(project=self.project).count(), 3)
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).title, "One")

    def test_task_paths(self):
        self.client.credentials()
        self.authenticate(self.user)
        task_id = self.task.task_id
        mention = "/api/v2/task/comment/mention/"
        self.assert_archived(
            self.post(
                "/api/v2/task/dependency/",
                {"blocker_task_id": task_id, "blocked_task_id": self.third.task_id},
            )
        )
        self.assert_archived(self.client.delete(f"/api/v2/task/dependency/{self.dependency.pk}/"))
        self.assert_archived(
            self.post(
                mention, {"team_id": self.team_id, "task_id": task_id, "mentioned_user_ids": []}
            )
        )
        self.assert_archived(
            self.client.delete(
                f"{mention}?team_id={self.team_id}&task_id={task_id}&comment_id=1"
                f"&mentioned_user_ids={self.user.id}"
            )
        )
        self.assert_archived(
            self.post(
                "/api/v2/task/body/attachment/",
                {
                    "task_id": task_id,
                    "uploader": str(self.user.id),
                    "body_attachment_file": SimpleUploadedFile("a.txt", b"x"),
                },
                format="multipart",
            )
        )
        self.assert_archived(
            self.post("/api/v2/task/activity/restore-body/", {"task_id": task_id, "activity_id": 1})
        )
        self.assert_archived(self.post("/api/v2/task/convert-to-milestone/", {"task_id": task_id}))
        self.assertTrue(TaskDependency.objects.filter(pk=self.dependency.pk).exists())

    def test_milestone_and_sprint_paths(self):
        self.client.credentials()
        self.authenticate(self.user)
        base = f"/api/v2/milestone/{self.milestone.milestone_id}/assignees/"
        self.assert_archived(self.post(base, {"user_id": str(self.user.id)}))
        self.assert_archived(self.client.delete(f"{base}{self.user.id}/"))
        self.assert_archived(
            self.post(
                "/api/v2/milestone/convert-to-task/", {"milestone_id": self.milestone.milestone_id}
            )
        )
        self.assert_archived(
            self.post(
                "/api/v2/sprint/config/",
                {"project_id": self.pid, "duration_days": 14, "anchor_date": "2026-06-01"},
            )
        )

    def test_project_setting_paths(self):
        self.client.credentials()
        self.authenticate(self.user)
        pid = self.pid
        tag = "/api/v2/project/tag/"
        tpl = "/api/v2/project/task-template/"
        flt = "/api/v2/project/saved-task-filters/"
        fields = "/api/v2/project/custom-fields/"
        calls = {
            "tag post": lambda: self.post(tag, {"project_id": pid, "tag_name": "x"}),
            "tag put": lambda: self.client.put(
                tag, {"project_id": pid, "old_tag_name": "x"}, format="json"
            ),
            "tag delete": lambda: self.client.delete(
                tag, {"project_id": pid, "tag_name": "x"}, format="json"
            ),
            "template post": lambda: self.post(
                tpl, {"project_id": pid, "template_name": "n", "body": []}
            ),
            "template put": lambda: self.client.put(
                tpl, {"id": 1, "project_id": pid}, format="json"
            ),
            "template delete": lambda: self.client.delete(
                tpl, {"id": 1, "project_id": pid}, format="json"
            ),
            "template defaults": lambda: self.client.put(
                f"{tpl}defaults/", {"project_id": pid, "kind": "task"}, format="json"
            ),
            "field rules": lambda: self.client.put(
                "/api/v2/project/task-field-rules/",
                {"project_id": pid, "rules": {}},
                format="json",
            ),
            "filter post": lambda: self.post(
                flt, {"project_id": pid, "filter_name": "n", "filters": {"status": ["Open"]}}
            ),
            "filter put": lambda: self.client.put(flt, {"id": 1, "project_id": pid}, format="json"),
            "filter delete": lambda: self.client.delete(
                flt, {"id": 1, "project_id": pid}, format="json"
            ),
            "custom field post": lambda: self.post(
                fields, {"project_id": pid, "field_name": "f", "field_type": "text"}
            ),
            "custom field put": lambda: self.client.put(
                fields, {"project_id": pid, "field_id": 1}, format="json"
            ),
            "custom field delete": lambda: self.client.delete(
                fields, {"project_id": pid, "field_id": 1}, format="json"
            ),
            "profile image": lambda: self.client.put(
                "/api/v2/project/profile/image/",
                {"project_id": pid, "profile_image": _png()},
                format="multipart",
            ),
        }
        for name, call in calls.items():
            with self.subTest(name):
                self.assert_archived(call())

    def test_a_malformed_task_id_is_the_views_own_refusal(self):
        """The guard reads the raw request value: text that is no task id must not be a 500."""
        self.client.credentials()
        self.authenticate(self.user)
        task_id = self.task.task_id
        mention = "/api/v2/task/comment/mention/"
        calls = {
            "dependency blocker": (
                404,
                lambda: self.post(
                    "/api/v2/task/dependency/",
                    {"blocker_task_id": "abc", "blocked_task_id": task_id},
                ),
            ),
            "dependency blocked": (
                404,
                lambda: self.post(
                    "/api/v2/task/dependency/",
                    {"blocker_task_id": task_id, "blocked_task_id": "abc"},
                ),
            ),
            "mention post": (
                404,
                lambda: self.post(
                    mention, {"team_id": self.team_id, "task_id": "abc", "mentioned_user_ids": []}
                ),
            ),
            "mention delete": (
                404,
                lambda: self.client.delete(
                    f"{mention}?team_id={self.team_id}&task_id=abc&comment_id=1"
                    f"&mentioned_user_ids={self.user.id}"
                ),
            ),
            "body attachment": (
                404,
                lambda: self.post(
                    "/api/v2/task/body/attachment/",
                    {
                        "task_id": "abc",
                        "uploader": str(self.user.id),
                        "body_attachment_file": SimpleUploadedFile("a.txt", b"x"),
                    },
                    format="multipart",
                ),
            ),
            "body restore": (
                400,
                lambda: self.post(
                    "/api/v2/task/activity/restore-body/", {"task_id": "abc", "activity_id": 1}
                ),
            ),
            "convert to milestone": (
                400,
                lambda: self.post("/api/v2/task/convert-to-milestone/", {"task_id": "abc"}),
            ),
        }
        for name, (expected, call) in calls.items():
            with self.subTest(name):
                response = call()
                self.assertEqual(response.status_code, expected, getattr(response, "data", None))
        self.assertIsNone(archived_task_response("abc"))


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return SimpleUploadedFile("p.png", buffer.getvalue(), content_type="image/png")

"""Chat-thread ↔ task linkage: one-task-per-thread guard, milestone
linkage persistence, crumb-context fields, by-thread lookup hygiene,
and the `repair_task_thread_links` command.

Background: `TaskMaster.chat_type/chat_id/thread_id` record which chat
thread a task was created from. The create paths now enforce that a
thread can be the origin of at most one live task, milestones persist
the same linkage on their backing task row, and the detail endpoints
carry the ancestry names (`milestoneTitle` / `parentTaskTitle`) the
thread-header breadcrumb renders.
"""

import uuid
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase


class ThreadLinkTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Linkage Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.DM,
            title="Linkage DM",
            owner=self.user,
        )
        self.thread_root = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={},
            body_text="root",
        )
        self.authenticate()

    def make_task(self, *, title="Linked task", thread=None, **extra):
        fields = dict(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title=title,
            status="Open",
        )
        if thread is not None:
            fields.update(
                chat_type=self.channel.kind,
                chat_id=str(self.channel.id),
                thread_id=str(thread.id),
            )
        fields.update(extra)
        return TaskMaster.objects.create(**fields)

    def finalize_put(self, task, thread):
        """Minimal create-form finalize PUT claiming `thread`."""
        return self.client.put(
            reverse("task_create"),
            {
                "task_id": task.task_id,
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "title": "Finalized",
                "chat_type": self.channel.kind,
                "chat_id": str(self.channel.id),
                "thread_id": str(thread.id),
            },
            format="json",
        )


class OneTaskPerThreadGuardTests(ThreadLinkTestBase):
    def test_put_claiming_linked_thread_is_rejected(self):
        existing = self.make_task(thread=self.thread_root)
        scaffold = self.make_task(title="Scaffold", is_init_task=True)

        resp = self.finalize_put(scaffold, self.thread_root)

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "thread_already_has_task")
        self.assertEqual(resp.data["existing_task_id"], existing.task_id)

    def test_put_resending_own_linkage_is_allowed(self):
        task = self.make_task(thread=self.thread_root)
        resp = self.finalize_put(task, self.thread_root)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_deleted_task_frees_the_thread(self):
        self.make_task(thread=self.thread_root, is_deleted=True)
        scaffold = self.make_task(title="Scaffold", is_init_task=True)
        resp = self.finalize_put(scaffold, self.thread_root)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_milestone_post_with_linked_thread_is_rejected(self):
        existing = self.make_task(thread=self.thread_root)
        resp = self.client.post(
            reverse("milestone_create"),
            {
                "project_id": self.project.project_id,
                "title": "Milestone from thread",
                "chat_type": self.channel.kind,
                "chat_id": str(self.channel.id),
                "thread_id": str(self.thread_root.id),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "thread_already_has_task")
        self.assertEqual(resp.data["existing_task_id"], existing.task_id)


class MilestoneThreadLinkageTests(ThreadLinkTestBase):
    def test_milestone_create_persists_linkage_and_serializes_it(self):
        resp = self.client.post(
            reverse("milestone_create"),
            {
                "project_id": self.project.project_id,
                "title": "Milestone from thread",
                "chat_type": self.channel.kind,
                "chat_id": str(self.channel.id),
                "thread_id": str(self.thread_root.id),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        payload = resp.data["milestone"]
        self.assertEqual(payload["chatType"], self.channel.kind)
        self.assertEqual(payload["chatId"], str(self.channel.id))
        self.assertEqual(payload["threadId"], str(self.thread_root.id))

        backing = TaskMaster.objects.get(task_id=payload["taskId"])
        self.assertEqual(backing.chat_id, str(self.channel.id))
        self.assertEqual(backing.thread_id, str(self.thread_root.id))
        self.assertEqual(backing.chat_type, self.channel.kind)

    def test_milestone_create_without_linkage_serializes_nulls(self):
        resp = self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "Plain milestone"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        payload = resp.data["milestone"]
        self.assertIsNone(payload["chatType"])
        self.assertIsNone(payload["chatId"])
        self.assertIsNone(payload["threadId"])


class GetTaskByThreadIdHygieneTests(ThreadLinkTestBase):
    def by_thread(self):
        query = (
            f"team_id={self.team.team_id}&chat_type={self.channel.kind}"
            f"&chat_id={self.channel.id}&thread_id={self.thread_root.id}"
        )
        return self.client.get(f"{reverse('get_task_by_thread_id')}?{query}")

    def test_deleted_task_is_not_served(self):
        self.make_task(thread=self.thread_root, is_deleted=True)
        resp = self.by_thread()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {})

    def test_duplicate_links_serve_oldest_instead_of_400(self):
        first = self.make_task(thread=self.thread_root, title="First claim")
        self.make_task(thread=self.thread_root, title="Second claim")
        resp = self.by_thread()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data[0]["id"], first.task_id)

    def test_response_carries_chat_and_crumb_fields(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Crumb milestone",
            status="Open",
        )
        self.make_task(thread=self.thread_root, milestone=milestone)
        resp = self.by_thread()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = resp.data[0]
        self.assertEqual(row["chatType"], self.channel.kind)
        self.assertEqual(row["chatId"], str(self.channel.id))
        self.assertEqual(row["threadId"], str(self.thread_root.id))
        self.assertEqual(row["milestoneTitle"], "Crumb milestone")


class GetTaskCrumbFieldTests(ThreadLinkTestBase):
    def test_get_task_returns_parent_and_milestone_titles(self):
        parent = self.make_task(title="Parent task")
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Ancestry milestone",
            status="Open",
        )
        child = self.make_task(
            title="Child task",
            parent_task_id=parent.task_id,
            milestone=milestone,
        )
        query = (
            f"team_id={self.team.team_id}&project_id={self.project.project_id}"
            f"&task_id={child.task_id}&attachments=meta"
        )
        resp = self.client.get(f"{reverse('get_task')}?{query}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        row = resp.data[0]
        self.assertEqual(row["milestoneTitle"], "Ancestry milestone")
        self.assertEqual(row["parentTaskTitle"], "Parent task")
        self.assertFalse(row["parentTaskIsMilestone"])


class RepairCommandTests(ThreadLinkTestBase):
    def run_repair(self, *args):
        out = StringIO()
        call_command("repair_task_thread_links", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_but_does_not_write(self):
        task = self.make_task(thread=self.thread_root)
        task.chat_id = str(uuid.uuid4())  # wrong channel
        task.save(update_fields=["chat_id"])

        self.run_repair()
        task.refresh_from_db()
        self.assertNotEqual(task.chat_id, str(self.channel.id))

    def test_wrong_channel_is_rewritten_from_message(self):
        task = self.make_task(thread=self.thread_root)
        task.chat_id = str(uuid.uuid4())
        task.chat_type = 3
        task.save(update_fields=["chat_id", "chat_type"])

        self.run_repair("--apply")
        task.refresh_from_db()
        self.assertEqual(task.chat_id, str(self.channel.id))
        self.assertEqual(task.chat_type, self.channel.kind)

    def test_reply_id_is_repointed_to_thread_root(self):
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body={},
            parent=self.thread_root,
            thread_root=self.thread_root,
            is_thread_reply=True,
        )
        task = self.make_task(thread=reply)

        self.run_repair("--apply")
        task.refresh_from_db()
        self.assertEqual(task.thread_id, str(self.thread_root.id))

    def test_dangling_link_is_cleared(self):
        task = self.make_task(thread=self.thread_root)
        task.thread_id = str(uuid.uuid4())  # no such message
        task.save(update_fields=["thread_id"])

        self.run_repair("--apply")
        task.refresh_from_db()
        self.assertIsNone(task.thread_id)
        self.assertIsNone(task.chat_id)
        self.assertIsNone(task.chat_type)

    def test_legacy_numeric_link_is_untouched(self):
        task = self.make_task()
        task.chat_type = 1
        task.chat_id = "123"
        task.thread_id = "456"
        task.save(update_fields=["chat_type", "chat_id", "thread_id"])

        self.run_repair("--apply")
        task.refresh_from_db()
        self.assertEqual(task.thread_id, "456")
        self.assertEqual(task.chat_id, "123")

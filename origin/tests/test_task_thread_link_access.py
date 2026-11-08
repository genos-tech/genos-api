"""A task can only be linked to a chat thread its writer can read (MIP-1314).

`TaskMaster.chat_id` / `thread_id` say which chat thread a task was made from. The create
form, the finalize PUT and the milestone POST took both ids from the body as they came, so
a project member who was not in a chat could name that chat's thread: the stranger's thread
then showed a task nobody there put on it, and a real task could no longer be made from the
thread (one live task per thread). The three REST writers now refuse a link to a chat the
writer cannot read, with the words of a thread that is not there, whatever the reason.

The in-app agent's `create_task` / `update_task` and the MCP tools of the same name never
took a chat or thread id; the last class pins that, so a future argument has to meet this
rule first.
"""

import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import McpBase

User = get_user_model()

NOT_FOUND = {"error": "Thread not found."}


class LinkAccessBase(BaseAPITestCase):
    """`self.user` writes. Their own DM is readable; the others' group chat is not."""

    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="third", email="third@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Linkage",
            code="LNK",
            owner=self.user,
            project_system_user=self.user,
        )
        for person in (self.user, self.user2, self.user3):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=person)
        self.mine, self.mine_root = self.chat([self.user, self.user2], "Mine")
        self.theirs, self.theirs_root = self.chat([self.user2, self.user3], "Theirs")
        self.authenticate()

    def chat(self, members, title, *, team=None, kind=ChannelKind.GM):
        channel = Channel.objects.create(team=team or self.team, kind=kind, title=title)
        for person in members:
            ChannelMember.objects.create(channel=channel, user=person)
        root = Message.objects.create(
            channel=channel, sender=members[0], seq=1, body={}, body_text="root"
        )
        return channel, root

    def link(self, channel, root):
        return {
            "chat_type": channel.kind,
            "chat_id": str(channel.id),
            "thread_id": str(root.id),
        }

    def scaffold(self, **extra):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Scaffold",
            status="Open",
            is_init_task=True,
            **extra,
        )

    def post_task(self, **link):
        return self.client.post(
            reverse("task_create"),
            {
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "assignee": str(self.user.id),
                "reporter": str(self.user.id),
                "title": "From a thread",
                "priority": "Normal",
                "effort_level": "Low",
                "status": "Open",
                "content": None,
                "due_date": None,
                "links": [],
                "tags": [],
                "is_init_task": False,
                **link,
            },
            format="json",
        )

    def put_task(self, task, **link):
        return self.client.put(
            reverse("task_create"),
            {
                "task_id": task.task_id,
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "title": "Finalized",
                **link,
            },
            format="json",
        )

    def post_milestone(self, **link):
        return self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "From a thread", **link},
            format="json",
        )

    def assertNothingLinked(self):
        linked = TaskMaster.objects.exclude(thread_id__isnull=True).exclude(thread_id="")
        self.assertEqual(list(linked.values_list("title", "thread_id")), [])


class TestCreateRefusesAThreadTheWriterCannotRead(LinkAccessBase):
    def test_the_create_form_cannot_name_a_chat_the_writer_is_not_in(self):
        resp = self.post_task(**self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertEqual(resp.data, NOT_FOUND)
        self.assertNothingLinked()

    def test_a_chat_they_read_with_a_thread_from_another_chat_is_refused(self):
        """Both ids must agree: a readable chat id cannot carry a stranger's thread."""
        link = self.link(self.mine, self.theirs_root)
        resp = self.post_task(**link)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNothingLinked()

    def test_a_thread_id_alone_is_judged_by_the_chat_it_sits_in(self):
        resp = self.post_task(thread_id=str(self.theirs_root.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNothingLinked()

    def test_a_chat_id_alone_is_judged_too(self):
        resp = self.post_task(chat_type=self.theirs.kind, chat_id=str(self.theirs.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNothingLinked()

    def test_a_chat_of_another_team_that_was_not_shared_is_refused(self):
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        chat, root = self.chat([self.user, self.user2], "Over there", team=other_team)
        resp = self.post_task(**self.link(chat, root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNothingLinked()

    def test_the_reasons_all_read_the_same(self):
        """Missing, unreadable, mismatched and malformed answer in the same words: the refusal
        cannot be used to learn that a thread exists or that a task sits on it."""
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user2,
            reporter=self.user2,
            title="Already on their thread",
            status="Open",
            **self.link(self.theirs, self.theirs_root),
        )
        ghost = uuid.uuid4()
        bodies = []
        for link in (
            self.link(self.theirs, self.theirs_root),  # unreadable, and already has a task
            {"chat_id": str(self.theirs.id), "thread_id": str(ghost)},  # no such message
            {"chat_id": str(ghost), "thread_id": str(ghost)},  # no such chat
            self.link(self.mine, self.theirs_root),  # ids from two chats
        ):
            resp = self.post_task(**link)
            self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, link)
            bodies.append(resp.data)
        self.assertEqual(bodies, [NOT_FOUND] * 4)

    def test_a_chat_the_writer_left_is_refused(self):
        ChannelMember.objects.filter(channel=self.mine, user=self.user).update(is_deleted=True)
        resp = self.post_task(**self.link(self.mine, self.mine_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)

    def test_their_own_chat_still_works(self):
        """The positive case: the denials above are not just a broken request."""
        resp = self.post_task(**self.link(self.mine, self.mine_root))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        task = TaskMaster.objects.get(task_id=resp.data["task"]["task_id"])
        self.assertEqual(task.chat_id, str(self.mine.id))
        self.assertEqual(task.thread_id, str(self.mine_root.id))

    def test_a_reply_in_their_chat_is_a_thread_they_can_link(self):
        reply = Message.objects.create(
            channel=self.mine,
            sender=self.user2,
            seq=2,
            body={},
            body_text="reply",
            thread_root_id=self.mine_root.id,
        )
        resp = self.post_task(**self.link(self.mine, reply))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_a_project_chat_is_read_through_the_project(self):
        """A project chat is read by being on the project (its channel rows only mirror that)."""
        pm = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        root = Message.objects.create(channel=pm, sender=self.user2, seq=1, body={}, body_text="x")
        resp = self.post_task(**self.link(pm, root))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_no_link_and_legacy_values_are_not_judged(self):
        """Nothing to join to: no ids, or the numbers and `-1` of rows from before chat v3."""
        for link in ({}, {"chat_type": -1, "chat_id": "-1", "thread_id": "-1"}, {"chat_id": "12"}):
            resp = self.post_task(**link)
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, (link, resp.data))


class TestFinalizeRefusesAThreadTheWriterCannotRead(LinkAccessBase):
    def test_a_put_cannot_claim_a_stranger_thread(self):
        task = self.scaffold()
        resp = self.put_task(task, **self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertEqual(resp.data, NOT_FOUND)
        task.refresh_from_db()
        self.assertIsNone(task.thread_id)
        self.assertEqual(task.title, "Scaffold", "a refused save changes nothing")

    def test_a_put_cannot_swap_in_a_stranger_chat_next_to_its_own_thread(self):
        task = self.scaffold(**self.link(self.mine, self.mine_root))
        resp = self.put_task(task, chat_id=str(self.theirs.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.chat_id, str(self.mine.id))

    def test_refusal_comes_before_the_one_task_per_thread_answer(self):
        """The "already linked" 400 names the other task: it must not be given to someone who
        cannot read the thread."""
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user2,
            reporter=self.user2,
            title="Already on their thread",
            status="Open",
            **self.link(self.theirs, self.theirs_root),
        )
        resp = self.put_task(self.scaffold(), **self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNotIn("existing_task_id", resp.data)

    def test_their_own_thread_can_be_claimed(self):
        task = self.scaffold()
        resp = self.put_task(task, **self.link(self.mine, self.mine_root))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.thread_id, str(self.mine_root.id))

    def test_a_link_that_is_already_there_is_not_judged_again(self):
        """Existing links are not touched: a writer who has since left the chat still saves."""
        task = self.scaffold(**self.link(self.theirs, self.theirs_root))
        resp = self.put_task(task, **self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.title, "Finalized")
        self.assertEqual(task.thread_id, str(self.theirs_root.id))

    def test_an_edit_without_link_fields_keeps_an_unreadable_link(self):
        task = self.scaffold(**self.link(self.theirs, self.theirs_root))
        resp = self.put_task(task)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.thread_id, str(self.theirs_root.id))


class TestMilestoneRefusesAThreadTheWriterCannotRead(LinkAccessBase):
    def test_a_milestone_cannot_be_made_from_a_stranger_thread(self):
        resp = self.post_milestone(**self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertEqual(resp.data, NOT_FOUND)
        self.assertNothingLinked()

    def test_refusal_comes_before_the_one_task_per_thread_answer(self):
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user2,
            reporter=self.user2,
            title="Already on their thread",
            status="Open",
            **self.link(self.theirs, self.theirs_root),
        )
        resp = self.post_milestone(**self.link(self.theirs, self.theirs_root))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND, resp.data)
        self.assertNotIn("existing_task_id", resp.data)

    def test_a_milestone_from_their_own_thread_keeps_the_link(self):
        resp = self.post_milestone(**self.link(self.mine, self.mine_root))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(
            TaskMaster.objects.get(thread_id=str(self.mine_root.id)).chat_id, str(self.mine.id)
        )


class TestTheToolsNeverTookALink(McpBase):
    """`create_task` and `update_task`, in the app's agent and over MCP, have no chat or thread
    argument. If one is ever added it has to go through the same readable-chat rule."""

    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="third", email="third@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.theirs = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Theirs")
        for person in (self.user2, self.user3):
            ChannelMember.objects.create(channel=self.theirs, user=person)
        self.root = Message.objects.create(
            channel=self.theirs, sender=self.user2, seq=1, body={}, body_text="root"
        )
        self.ids = {"chat_id": str(self.theirs.id), "thread_id": str(self.root.id)}
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_the_tool_declarations_have_no_such_argument(self):
        for tool in (CREATE_TASK, UPDATE_TASK):
            declared = set(tool.parameters_schema.get("properties", {}))
            self.assertFalse(declared & {"chat_id", "thread_id", "chat_type"}, tool.name)

    def test_the_agent_create_task_stores_no_link_even_when_asked(self):
        out = CREATE_TASK.run(
            {"title": "T", "project_id": self.project.project_id, **self.ids}, self.ctx
        )
        task = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertFalse(task.thread_id)
        self.assertFalse(task.chat_id)

    def test_the_agent_update_task_stores_no_link_even_when_asked(self):
        UPDATE_TASK.run({"task_id": self.task.task_id, "title": "Renamed", **self.ids}, self.ctx)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Renamed")
        self.assertFalse(self.task.thread_id)
        self.assertFalse(self.task.chat_id)

    def test_mcp_refuses_the_argument_and_changes_nothing(self):
        self._key()
        for name, args in (
            ("create_task", {"title": "T", "project_id": self.project.project_id}),
            ("update_task", {"task_id": self.task.task_id, "title": "Renamed"}),
        ):
            text, is_error = self._tool_text(self._call(name, {**args, **self.ids}))
            self.assertTrue(is_error, (name, text))
            self.assertIn("thread_id", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Fix the login redirect")
        self.assertFalse(TaskMaster.objects.exclude(thread_id__isnull=True).exclude(thread_id=""))

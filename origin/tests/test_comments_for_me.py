"""`list_my_waiting_comments` (MIP-2485): the task comments the member has not answered.

Comments are written through the real fan-out, so mentions and unread rows are what the product
writes. What matters: only open work that involves the member, only what they neither answered nor
reacted to, nothing they cannot open, nothing marked read, and a fixed number of queries.
"""

from __future__ import annotations

import json

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Activity, Message, MessageReaction
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskCommentReactionFact
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.tools.base import (
    MCP_ONLY_REGISTRY,
    REGISTRY,
    ToolContext,
    ToolError,
    get_app_tool,
)
from origin.services import comments_for_me
from origin.tests.test_unread_digest import UnreadBase

NAME = "list_my_waiting_comments"


class WaitingBase(UnreadBase):
    def waiting(self, ctx=None, **args):
        return comments_for_me.build_comments_for_me(ctx or self.ctx, **args)

    def mine(self, task, text="On it"):
        return self.task_comment(task, text, sender=self.user)

    def ids(self, result):
        return [row["task_id"] for row in result["tasks"]]

    def texts(self, row):
        return [unwrap(c["text"]) for c in row["comments"]]

    def react(self, task, comment, *, who=None):
        TaskCommentReactionFact.objects.create(
            team=self.team,
            task=task,
            comment_id=comment.comment_id,
            reaction_id=1,
            reaction_emoji="👍",
            sender=who or self.user,
            uid=f"{task.task_id}-{comment.comment_id}-1",
        )


class WhatWaitsTests(WaitingBase):
    def test_a_comment_nobody_answered_waits_whatever_the_task_status(self):
        for status in ("Open", "WIP", "Blocked", "Pending"):
            task = self.make_task(f"{status} task", status=status)
            self.task_comment(task, f"Can you check {status}?")
        result = self.waiting()
        self.assertEqual(result["stats"], {"tasks": 4, "comments": 4, "complete": True})
        row = result["tasks"][0]
        self.assertEqual(row["your_roles"], ["assignee"])
        self.assertEqual(self.texts(row), ["Can you check Pending?"])  # newest first
        self.assertTrue(row["comments"][0]["unread"])

    def test_closed_and_deleted_work_is_left_out(self):
        for status in ("Closed", "Deleted"):
            self.task_comment(self.make_task(status, status=status), "Still there?")
        child_of_closed = self.make_task(
            "child", parent_task_id=self.make_task("done", status="Closed").task_id
        )
        self.task_comment(child_of_closed, "Still there?")
        with override_settings(
            SEARCH_ENGINE={**self.search_engine(), "AGENT_CLOSED_ANCESTOR_RULE": True}
        ):
            self.assertEqual(self.waiting()["tasks"], [])

    def search_engine(self):
        from django.conf import settings  # noqa: PLC0415

        return settings.SEARCH_ENGINE

    def test_my_later_comment_answers_what_came_before_it_only(self):
        task = self.make_task()
        self.task_comment(task, "First question?")
        self.mine(task, "Answered the first")
        self.task_comment(task, "Second question?")
        row = self.waiting()["tasks"][0]
        self.assertEqual(self.texts(row), ["Second question?"])
        self.assertEqual(unwrap(row["your_last_comment"]), "Answered the first")
        self.mine(task, "Answered the second")
        self.assertEqual(self.waiting()["tasks"], [])

    def test_my_reaction_answers_that_comment_only(self):
        task = self.make_task()
        first = self.task_comment(task, "FYI the build is green")
        self.task_comment(task, "Which colour do you want?")
        self.react(task, first)
        self.assertEqual(self.texts(self.waiting()["tasks"][0]), ["Which colour do you want?"])

    def test_someone_elses_reaction_does_not(self):
        task = self.make_task()
        comment = self.task_comment(task, "Which colour?")
        self.react(task, comment, who=self.user2)
        self.assertEqual(self.waiting()["stats"]["comments"], 1)

    def test_my_reaction_on_the_chat_copy_of_a_comment_counts_too(self):
        task = self.make_task()
        comment = self.task_comment(task, "Which colour?")
        mirror = Message.objects.get(task=task, metadata__taskCommentId=comment.comment_id)
        MessageReaction.objects.create(message=mirror, user=self.user, emoji="👍")
        self.assertEqual(self.waiting()["tasks"], [])

    def test_a_reply_typed_in_the_tasks_chat_thread_answers_too(self):
        task = self.make_task()
        self.task_comment(task, "Which colour?")
        mirror = Message.objects.get(task=task, metadata__has_key="taskCommentId")
        root = mirror.thread_root
        self.send(root.channel, "Blue", sender=self.user, parent=root)
        self.assertEqual(self.waiting()["tasks"], [])

    def test_my_own_comment_alone_is_not_waiting(self):
        self.mine(self.make_task())
        self.assertEqual(self.waiting()["tasks"], [])

    def test_every_way_of_being_involved_and_no_other(self):
        other = self.user2
        reported = self.make_task("reported", assignee=other, reporter=self.user)
        shared = self.make_task("shared", assignee=other)
        shared.collaborators.add(self.user)
        mentioned = self.make_task("mentioned", assignee=other)
        unrelated = self.make_task("unrelated", assignee=other)
        for task in (reported, shared, unrelated):
            self.task_comment(task, "Thoughts?")
        self.task_comment(
            mentioned,
            body=[
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "mention", "props": {"user": "me", "userId": self.me}},
                        {"type": "text", "text": " can you look?"},
                    ],
                }
            ],
        )
        result = self.waiting()
        roles = {row["task_id"]: row["your_roles"] for row in result["tasks"]}
        self.assertEqual(
            roles,
            {
                reported.task_id: ["reporter"],
                shared.task_id: ["collaborator"],
                mentioned.task_id: ["mentioned"],
            },
        )
        row = next(r for r in result["tasks"] if r["task_id"] == mentioned.task_id)
        self.assertTrue(row["comments"][0]["mentions_you"])

    def test_a_project_the_member_left_gives_nothing(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Private", code="PRV", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        task = self.make_task("secret", assignee=self.user2, project=project)
        task.collaborators.add(self.user)
        self.task_comment(task, "Secret question?")
        result = self.waiting()
        self.assertEqual((result["tasks"], result["stats"]["comments"]), ([], 0))
        self.assertNotIn("Secret", self.everything(result))

    def test_a_deleted_comment_and_an_old_one_do_not_count(self):
        task = self.make_task()
        gone = self.task_comment(task, "never mind")
        gone.is_deleted = True
        gone.save()
        self.comment(task, self.user2, "last year", at=self.ago(days=40))
        self.assertEqual(self.waiting()["tasks"], [])
        self.assertEqual(self.waiting(days=60)["stats"]["comments"], 1)

    def test_nothing_is_marked_read(self):
        self.task_comment(self.make_task())
        before = Activity.objects.filter(recipient=self.user, is_read=False).count()
        self.assertGreater(before, 0)
        self.waiting()
        self.assertEqual(
            Activity.objects.filter(recipient=self.user, is_read=False).count(), before
        )


class ShapeTests(WaitingBase):
    def test_pages_and_one_task(self):
        tasks = [self.make_task(f"t{i}") for i in range(3)]
        for task in tasks:
            self.task_comment(task, "Ping?")
        first = self.waiting(limit=2)
        self.assertEqual(first["next_offset"], 2)
        rest = self.waiting(limit=2, offset=first["next_offset"])
        self.assertNotIn("next_offset", rest)
        self.assertEqual(
            sorted(self.ids(first) + self.ids(rest)), sorted(t.task_id for t in tasks)
        )
        self.assertEqual(self.ids(self.waiting(task_id=tasks[1].task_id)), [tasks[1].task_id])
        with self.assertRaises(ToolError):
            self.waiting(task_id="PRJ")

    def test_a_long_run_of_comments_keeps_the_newest_and_says_so(self):
        task = self.make_task()
        for i in range(comments_for_me.COMMENTS_PER_TASK + 2):
            self.task_comment(task, f"note {i}")
        row = self.waiting()["tasks"][0]
        self.assertEqual(row["comments_omitted"], 2)
        self.assertEqual(self.texts(row)[-1], f"note {comments_for_me.COMMENTS_PER_TASK + 1}")

    def test_only_an_outside_reader_gets_web_addresses(self):
        task = self.make_task()
        comment = self.task_comment(task, "Ping?")
        self.assertNotIn("url", self.waiting()["tasks"][0])
        mcp = ToolContext(team_id=self.ctx.team_id, user_id=self.me, surface="mcp")
        with override_settings(FRONTEND_BASE_URL="https://app.example.com/"):
            row = self.waiting(ctx=mcp)["tasks"][0]
        base = (
            f"https://app.example.com/workspace/tasks/project/{task.project_id}"
            f"/task/{task.task_id}"
        )
        self.assertEqual(row["url"], base)
        self.assertEqual(row["comments"][0]["url"], f"{base}/comment/{comment.comment_id}")

    def test_the_query_count_does_not_grow(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.waiting()
            return len(ctx.captured_queries)

        task = self.make_task("one")
        self.task_comment(task, "Ping?")
        self.mine(task, "ok")
        self.task_comment(task, "Again?")
        one = count()
        for i in range(6):
            more = self.make_task(f"more {i}")
            self.task_comment(more, "Ping?")
            self.mine(more, "ok")
            self.task_comment(more, "Again?")
        self.assertEqual(count(), one)


class ReviewFixTests(WaitingBase):
    def app_row(self, task, comment, kind):
        TaskActivity.objects.create(
            team=self.team,
            project=task.project,
            task=task,
            actor=self.user,
            action_type=TaskActivityActionType.COMMENT_ADDED,
            metadata={"commentId": comment.comment_id, "via": {"kind": kind}},
        )

    def test_my_ai_apps_comment_is_not_my_answer_but_is_handed_over(self):
        for kind in ("oauth", "api_key"):
            task = self.make_task(kind)
            self.task_comment(task, "Approve this?")
            self.app_row(task, self.mine(task, "Agent: build is green"), kind)
            row = self.waiting(task_id=task.task_id)["tasks"][0]
            self.assertEqual(self.texts(row), ["Approve this?"])
            self.assertEqual(unwrap(row["your_ai_app_comment"]), "Agent: build is green")
            self.assertNotIn("your_last_comment", row)
        for kind in ("app", "agent"):  # typed by the member, or approved by them in Genos
            task = self.make_task(kind)
            self.task_comment(task, "Approve this?")
            self.app_row(task, self.mine(task, "Yes"), kind)
            self.assertEqual(self.waiting(task_id=task.task_id)["tasks"], [])

    def test_a_second_milestone_assignee_is_involved(self):
        backing = self.make_task("The milestone", assignee=self.user2, is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="Launch"
        )
        MilestoneAssignees.objects.create(team=self.team, milestone=milestone, user=self.user)
        self.task_comment(backing, "Are we on time?")
        row = self.waiting()["tasks"][0]
        self.assertEqual(row["your_roles"], ["assignee"])
        self.assertEqual(row["milestone_id"], milestone.milestone_id)

    def test_the_first_question_survives_a_long_run(self):
        task = self.make_task()
        for i in range(comments_for_me.COMMENTS_PER_TASK + 3):
            self.task_comment(task, f"note {i}")
        texts = self.texts(self.waiting()["tasks"][0])
        self.assertEqual((len(texts), texts[0], texts[1]), (8, "note 0", "note 4"))

    def test_a_large_answer_is_cut_before_the_cap_and_pages_without_a_gap(self):
        tasks = [self.make_task(f"big {i}") for i in range(12)]
        for task in tasks:
            for _ in range(4):
                self.task_comment(task, "x" * 700)
        seen, offset = [], 0
        while offset is not None:
            page = self.waiting(limit=50, offset=offset)
            self.assertLess(len(json.dumps(page["tasks"], indent=2)), 24_000)
            seen += self.ids(page)
            offset = page.get("next_offset")
        self.assertEqual(sorted(seen), sorted(t.task_id for t in tasks))


class PublishingTests(WaitingBase):
    def test_in_the_app_it_exists_only_behind_its_flag_and_over_mcp_always(self):
        from django.conf import settings  # noqa: PLC0415

        self.assertNotIn(NAME, REGISTRY)
        self.assertIn(NAME, MCP_ONLY_REGISTRY)
        flag = "AGENT_INAPP_WAITING_COMMENTS_TOOL"
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, flag: False}):
            self.assertIsNone(get_app_tool(NAME))
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, flag: True}):
            self.assertFalse(get_app_tool(NAME).requires_approval)
        self.task_comment(self.make_task(), "Ping?")
        text, is_error = self._tool_text(self._call(NAME, {}))
        self.assertFalse(is_error, text)
        self.assertIn("/comment/", text)


class UnreadNamesAndLinksTests(UnreadBase):
    """What `list_my_unread` gained for the skill `unread-summary`."""

    def test_an_outside_reader_pages_through_every_chat_and_links_each_message(self):
        channels = [self.channel([self.user, self.user2], title=f"c{i}") for i in range(14)]
        for channel in channels:
            for _ in range(5):
                self.send(channel, "y" * 900)
        mcp = ToolContext(team_id=self.ctx.team_id, user_id=self.me, surface="mcp")
        seen, offset, pages = [], 0, 0
        while offset is not None:
            page = self.unread(ctx=mcp, limit=300, offset=offset)
            self.assertLess(len(json.dumps(page["items"], indent=2)), 24_000)
            for item in page["items"]:
                seen.append(item["chat_id"])
                event = item["events"][-1]
                self.assertEqual(event["url"], f"{item['url']}/message/5")
                self.assertEqual(len(unwrap(event["text"])), 500)
            offset, pages = page.get("next_offset"), pages + 1
        self.assertGreater(pages, 1)
        self.assertEqual(sorted(seen), sorted(str(c.id) for c in channels))
        # One chat asked for: the messages are whole. In the app nothing is cut for size.
        one = self.unread(ctx=mcp, chat_id=str(channels[0].id))
        self.assertEqual(len(unwrap(one["items"][0]["events"][0]["text"])), 900)
        app = self.unread(limit=300)
        self.assertEqual(len(app["items"]), 14)
        self.assertNotIn("next_offset", app)
        with self.assertRaises(ToolError):
            self.unread(offset="x")

    def test_a_chat_item_carries_its_name_and_over_mcp_its_address(self):
        named = self.channel([self.user, self.user2], title="Design")
        root = self.send(named, "root", sender=self.user)
        self.send(named, "reply", parent=root)
        self.send(named, "hello")
        app = self.unread()
        for item in app["items"]:
            self.assertEqual(unwrap(item["title"]), "Design")
            self.assertNotIn("url", item)
        mcp = ToolContext(team_id=self.ctx.team_id, user_id=self.me, surface="mcp")
        with override_settings(FRONTEND_BASE_URL="https://app.example.com"):
            items = {i["type"]: i for i in self.unread(ctx=mcp)["items"]}
        base = f"https://app.example.com/workspace/chat/gm/{named.id}"
        self.assertEqual(items["thread"]["url"], f"{base}/thread/{root.id}")
        self.assertEqual(items["chat"]["url"], base)

    def test_a_task_item_carries_its_project(self):
        task = self.make_task()
        self.task_comment(task, "Ping?")
        item = self.item(self.unread(), type="task")
        self.assertEqual(item["project_id"], task.project_id)

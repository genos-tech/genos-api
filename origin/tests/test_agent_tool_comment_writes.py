"""`update_comment` and `delete_comment` (MIP-880).

`add_comment` was one-way: an agent that mistyped a comment, or put it on the
wrong task, could only add another, and over MCP there is no approval card to
catch it first. These pin what the two tools share with the page and what is
theirs:

* **Only your own.** As on the page, someone else's comment is answered exactly
  like a missing one, so the tool never says a comment exists that you can't
  touch.
* **One implementation.** The edit reaches the PM thread's copy and the delete
  tombstones it, because the page and the tools call the same service.
* **An edit tells only the newly mentioned.** A person already named in the
  comment is not pinged again for a typo fix.
* **A delete is soft,** so `comment_id` (a per-task sequence) is never reused.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.delete_comment import DELETE_COMMENT
from origin.search_engine.agent.tools.update_comment import UPDATE_COMMENT
from origin.services.unified_writer import task_comment_message_uuid

from .test_base import BaseAPITestCase

User = get_user_model()


def plain(body) -> str:
    """The text of a BlockNote body, blocks joined by newlines."""
    return "\n".join(
        "".join(span.get("text", "") for span in block.get("content") or []) for block in body or []
    ).strip()


class CommentToolBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user2,
            title="Implementation",
            status="WIP",
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))

    def add(self, text="first draft", ctx=None):
        return ADD_COMMENT.run({"task_id": self.task.task_id, "body_text": text}, ctx or self.ctx)[
            "comment_id"
        ]

    def edit(self, comment_id, text, ctx=None):
        return UPDATE_COMMENT.run(
            {"task_id": self.task.task_id, "comment_id": comment_id, "body_text": text},
            ctx or self.ctx,
        )

    def remove(self, comment_id, ctx=None):
        return DELETE_COMMENT.run(
            {"task_id": self.task.task_id, "comment_id": comment_id}, ctx or self.ctx
        )

    def row(self, comment_id) -> TaskComments:
        return TaskComments.objects.get(task=self.task, comment_id=comment_id)

    def mirror(self, comment_id) -> Message:
        return Message.objects.get(id=task_comment_message_uuid(self.task.task_id, comment_id))

    def mentions(self, user):
        return Activity.objects.filter(recipient=user, activity_type=ActivityType.MENTION).count()


class TheToolsAreApprovalGatedWrites(CommentToolBase):
    def test_both_need_approval_and_name_what_they_take(self):
        for tool, required in (
            (UPDATE_COMMENT, {"task_id", "comment_id", "body_text"}),
            (DELETE_COMMENT, {"task_id", "comment_id"}),
        ):
            self.assertTrue(tool.requires_approval, tool.name)
            self.assertEqual(set(tool.parameters_schema["required"]), required, tool.name)
            self.assertIs(REGISTRY[tool.name], tool)


class UpdateCommentTests(CommentToolBase):
    def test_the_author_replaces_the_text(self):
        cid = self.add("first draft")

        result = self.edit(cid, "Fixed **for real**")

        self.assertEqual(result["comment_id"], cid)
        self.assertEqual(plain(self.row(cid).comment_body), "Fixed for real")
        self.assertIn("Edited comment", result["__summary__"])

    def test_the_pm_thread_shows_the_new_text_too(self):
        cid = self.add("first draft")

        self.edit(cid, "second draft")

        mirror = self.mirror(cid)
        self.assertEqual(mirror.body_text, "second draft")
        self.assertIsNotNone(mirror.edited_at)

    def test_the_edit_is_recorded_in_the_task_history(self):
        cid = self.add("first draft")
        self.edit(cid, "second draft")
        self.assertTrue(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.COMMENT_EDITED
            ).exists()
        )

    def test_it_trims_and_refuses_an_empty_text(self):
        cid = self.add("first draft")
        with self.assertRaises(ToolError):
            self.edit(cid, "   ")
        self.assertEqual(plain(self.row(cid).comment_body), "first draft")

    def test_someone_else_cannot_edit_it(self):
        cid = self.add("mine")
        with self.assertRaises(ToolError) as caught:
            self.edit(cid, "now it is yours", ctx=self.ctx2)
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(plain(self.row(cid).comment_body), "mine")
        self.assertEqual(self.mirror(cid).body_text, "mine")

    def test_a_deleted_comment_cannot_be_edited(self):
        cid = self.add("gone soon")
        self.remove(cid)
        with self.assertRaises(ToolError):
            self.edit(cid, "back again")

    def test_a_comment_that_does_not_exist(self):
        with self.assertRaises(ToolError):
            self.edit(99, "hello")

    def test_the_comment_id_must_be_a_number(self):
        with self.assertRaises(ToolError):
            self.edit("abc", "hello")

    def test_a_comment_on_another_task_is_not_found(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Other", status="Open"
        )
        cid = self.add("on the first task")
        with self.assertRaises(ToolError):
            UPDATE_COMMENT.run(
                {"task_id": other.task_id, "comment_id": cid, "body_text": "moved"}, self.ctx
            )
        self.assertEqual(plain(self.row(cid).comment_body), "on the first task")

    def test_a_newly_mentioned_person_is_notified_once(self):
        cid = self.add("no one named yet")
        self.assertEqual(self.mentions(self.user2), 0)
        chip = f"[@Other](user:{self.user2.id})"

        result = self.edit(cid, f"Over to {chip} please")

        self.assertEqual(result["newly_mentioned"], 1)
        self.assertEqual(self.mentions(self.user2), 1)
        # A second edit that keeps the same mention and changes the words does
        # not ping them again.
        again = self.edit(cid, f"Over to {chip}, thanks")
        self.assertEqual(again["newly_mentioned"], 0)
        self.assertEqual(self.mentions(self.user2), 1)

    def test_an_edit_that_names_no_one_notifies_no_one(self):
        cid = self.add("first draft")
        before = Activity.objects.count()
        result = self.edit(cid, "just a typo fix")
        self.assertEqual(result["newly_mentioned"], 0)
        self.assertEqual(Activity.objects.count(), before)

    def test_the_edit_survives_a_thread_that_cannot_be_updated(self):
        cid = self.add("first draft")
        with patch(
            "origin.services.task_comment_manage.unified_writer.update_task_comment_thread_reply",
            return_value=None,
        ):
            self.edit(cid, "second draft")
        self.assertEqual(plain(self.row(cid).comment_body), "second draft")


class AccessTests(CommentToolBase):
    def setUp(self):
        super().setUp()
        self.cid = self.add("first draft")
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.in_team_not_project = User.objects.create_user(
            username="bystander", email="bystander@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.in_team_not_project)

    def test_a_user_in_another_team_is_refused(self):
        ctx = ToolContext(team_id=str(self.other_team.team_id), user_id=str(self.outsider.id))
        with self.assertRaises(ToolForbidden):
            self.edit(self.cid, "hijacked", ctx=ctx)
        with self.assertRaises(ToolForbidden):
            self.remove(self.cid, ctx=ctx)
        self.assertEqual(plain(self.row(self.cid).comment_body), "first draft")
        self.assertFalse(self.row(self.cid).is_deleted)

    def test_a_member_acting_for_another_team_is_refused(self):
        """The ACL alone would let this through: they ARE a member of the task's
        project, but the run is scoped to a different team, and the tools hold to
        the run's team."""
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        ctx = ToolContext(team_id=str(self.other_team.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolForbidden) as caught:
            self.edit(self.cid, "from the wrong team", ctx=ctx)
        self.assertIn("different team", str(caught.exception))
        with self.assertRaises(ToolForbidden):
            self.remove(self.cid, ctx=ctx)
        self.assertEqual(plain(self.row(self.cid).comment_body), "first draft")
        self.assertFalse(self.row(self.cid).is_deleted)

    def test_a_teammate_who_cannot_see_the_task_is_refused(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.in_team_not_project.id))
        with self.assertRaises(ToolForbidden):
            self.edit(self.cid, "hijacked", ctx=ctx)
        with self.assertRaises(ToolForbidden):
            self.remove(self.cid, ctx=ctx)

    def test_a_deleted_task_is_refused(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self.edit(self.cid, "hello")
        with self.assertRaises(ToolError):
            self.remove(self.cid)


class DeleteCommentTests(CommentToolBase):
    def test_it_soft_deletes_and_counts_what_is_left(self):
        first = self.add("first")
        self.add("second")

        result = self.remove(first)

        self.assertTrue(result["deleted"])
        self.assertFalse(result["already_deleted"])
        self.assertEqual(result["comments_left"], 1)
        self.assertTrue(self.row(first).is_deleted, "soft, not removed")

    def test_the_thread_copy_is_tombstoned_and_the_count_drops(self):
        first = self.add("first")
        self.add("second")
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 2)

        self.remove(first)

        self.assertIsNotNone(self.mirror(first).deleted_at)
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 1)

    def test_the_search_index_forgets_it(self):
        cid = self.add("first")
        with patch("origin.services.task_comment_manage.purge_task_comment") as purge:
            self.remove(cid)
        purge.assert_called_once_with(self.task.task_id, cid)

    def test_deleting_twice_is_harmless(self):
        cid = self.add("first")
        self.remove(cid)
        deleted_events = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
        ).count()

        again = self.remove(cid)

        self.assertTrue(again["already_deleted"])
        self.assertIn("already deleted", again["__summary__"])
        self.assertEqual(
            TaskActivity.objects.filter(
                task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
            ).count(),
            deleted_events,
        )
        self.header.refresh_from_db()
        self.assertEqual(self.header.reply_count, 0)

    def test_the_next_comment_does_not_reuse_the_id(self):
        cid = self.add("first")
        self.remove(cid)
        self.assertEqual(self.add("second"), cid + 1)

    def test_someone_else_cannot_delete_it(self):
        cid = self.add("mine")
        with self.assertRaises(ToolError) as caught:
            self.remove(cid, ctx=self.ctx2)
        self.assertIn("not found", str(caught.exception))
        self.assertFalse(self.row(cid).is_deleted)
        self.assertIsNone(self.mirror(cid).deleted_at)

    def test_a_comment_that_does_not_exist(self):
        with self.assertRaises(ToolError):
            self.remove(99)

"""Comments and @mentions made through MCP or the in-app agent reach the recipient's open app live
(MIP-1660).

The app's own writes go through a sockets handler, which calls the API and emits the activities it
hands back. An MCP or agent write calls the same services with no handler behind it, so the comment and
mention rows were stored and announced to nobody: no toast, and the Activity feed filled in only at the
next refetch (measured in a browser with two members). `realtime_push.push_activities` now sends them.

Pinned: what is sent (one push per recipient, to `/internal/broadcast/activity`, after commit only),
that a recipient who cannot open the task gets the minimal card and never the comment text (MIP-1593),
that every agent/MCP path pushes (`add_comment`, `update_comment` adding a mention, an @mention in a task
body or a note) and that the app's own REST path, which sockets relays, does NOT (no double delivery),
and that an outage never fails the write.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskComments
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.update_comment import UPDATE_COMMENT
from origin.services import realtime_push
from origin.tests.test_mention_group_recipients import TaskFixture

User = get_user_model()

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
ACTIVITY_URL = f"{SOCKETS}{realtime_push.ACTIVITY_PATH}"


def mention(user) -> str:
    return f"[@{user.username}](user:{user.id})"


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class Base(TaskFixture):
    def setUp(self):
        super().setUp()
        # The colleague on the project, and `self.member`: on the team, NOT on the project.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)
        # The Web Push side has its own tests; keep it out of the way.
        for target in (
            "origin.services.task_comment_fanout.schedule_push_for_activities",
            "origin.services.webpush_dispatch.schedule_push_for_activities",
        ):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)

    def run_tool(self, tool, **args):
        with self.captureOnCommitCallbacks(execute=True):
            return tool.run(args, self.ctx)

    def activity_pushes(self):
        """`(recipient, activity)` for each push to the activity route, in order."""
        out = []
        for call in self.post.call_args_list:
            if call.args[0] == ACTIVITY_URL:
                body = json.loads(call.kwargs["data"])
                out.append((body["userId"], body["activity"]))
        return out


class AddCommentTests(Base):
    def test_the_assignee_is_told_live_about_a_plain_comment(self):
        self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text="looks good")
        pushes = self.activity_pushes()
        self.assertEqual([r for r, _ in pushes], [str(self.user.id)])
        recipient, activity = pushes[0]
        self.assertEqual(activity["recipientUserId"], recipient)
        self.assertEqual(activity["message"]["metadata"]["taskCommentId"], 1)
        self.assertEqual(activity["message"]["taskId"], self.task.task_id)

    def test_an_at_mention_is_told_live(self):
        self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text=f"{mention(self.user)} see")
        kinds = {(r, a["activityType"]) for r, a in self.activity_pushes()}
        self.assertIn((str(self.user.id), 3), kinds)

    def test_a_recipient_who_cannot_open_the_task_gets_the_minimal_card(self):
        # `self.member` is on the team but not on the project: a direct mention still reaches them
        # (MIP-1061), and what they are shown is the minimal card (MIP-1593).
        # The card keeps the comment's own text (the sender wrote it to them) and drops everything
        # about the task: its title, project, ids and status (`activity_minimal_card`).
        self.task.title = "Codename Falcon"
        self.task.save(update_fields=["title"])
        self.run_tool(
            ADD_COMMENT,
            task_id=self.task.task_id,
            body_text=f"{mention(self.member)} please review",
        )
        mine = [a for r, a in self.activity_pushes() if r == str(self.member.id)]
        self.assertEqual(len(mine), 1)
        self.assertIs(mine[0]["meta"].get("noAccess"), True)
        for key in ("taskId", "displayId", "taskStatus", "taskCommentCount"):
            self.assertIsNone(mine[0]["message"].get(key), key)
        self.assertNotIn("Codename Falcon", json.dumps(mine[0]))
        # A project member gets the full row.
        full = [a for r, a in self.activity_pushes() if r == str(self.user.id)]
        self.assertEqual(full[0]["message"]["taskId"], self.task.task_id)

    def test_nothing_is_sent_before_the_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            ADD_COMMENT.run({"task_id": self.task.task_id, "body_text": "x"}, self.ctx)
        self.assertEqual(self.activity_pushes(), [])
        self.assertTrue(callbacks)

    def test_an_outage_never_fails_the_comment(self):
        self.post.side_effect = realtime_push.requests.ConnectionError("down")
        out = self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text="still saved")
        self.assertTrue(
            TaskComments.objects.filter(task=self.task, comment_id=out["comment_id"]).exists()
        )

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_unconfigured_is_a_no_op(self):
        self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text="x")
        self.assertEqual(self.activity_pushes(), [])


class UpdateCommentTests(Base):
    def test_a_mention_added_by_an_edit_is_told_live(self):
        out = self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text="first")
        self.post.reset_mock()
        self.run_tool(
            UPDATE_COMMENT,
            task_id=self.task.task_id,
            comment_id=out["comment_id"],
            body_text=f"first {mention(self.user)}",
        )
        kinds = [(r, a["activityType"]) for r, a in self.activity_pushes()]
        self.assertEqual(kinds, [(str(self.user.id), 3)])


class BodyMentionTests(Base):
    def test_a_mention_in_a_task_body_is_told_live(self):
        self.task.content = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "props": {"userId": str(self.user.id), "userName": self.user.username},
                    }
                ],
            }
        ]
        self.task.save(update_fields=["content"])
        with self.captureOnCommitCallbacks(execute=True):
            notify_task_body_mentions(self.task, actor_id=str(self.user2.id))
        pushes = self.activity_pushes()
        self.assertEqual([r for r, _ in pushes], [str(self.user.id)])
        self.assertEqual(pushes[0][1]["surfaceType"], 5)

    def test_a_re_save_with_the_same_mention_pushes_nothing_again(self):
        self.test_a_mention_in_a_task_body_is_told_live()
        self.post.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            notify_task_body_mentions(self.task, actor_id=str(self.user2.id))
        self.assertEqual(self.activity_pushes(), [])


class TheAppsOwnPathIsNotDoubledTests(Base):
    def test_a_comment_typed_in_the_app_is_left_to_sockets(self):
        # The REST view the sockets `task_comment` handler calls hands the activities back for
        # sockets to emit; pushing them from here too would deliver each twice.
        # `comment` posts to the REST view and asserts the 201 itself.
        with self.captureOnCommitCallbacks(execute=True):
            self.comment(
                [{"type": "paragraph", "content": [{"type": "text", "text": "typed in the app"}]}]
            )
        self.assertEqual(self.activity_pushes(), [])


class PushActivitiesTests(Base):
    def test_nothing_to_send_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_activities([])
            realtime_push.push_activities(None)
        self.post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_a_missing_secret_sends_nothing(self):
        self.run_tool(ADD_COMMENT, task_id=self.task.task_id, body_text="x")
        self.assertEqual(self.activity_pushes(), [])

    def test_a_refusal_by_sockets_is_logged_and_does_not_stop_the_rest(self):
        self.post.return_value = mock.Mock(status_code=413)
        with self.assertLogs("origin.services.realtime_push", "WARNING"):
            self.run_tool(
                ADD_COMMENT,
                task_id=self.task.task_id,
                body_text=f"{mention(self.user)} {mention(self.member)}",
            )
        self.assertGreaterEqual(len(self.activity_pushes()), 2)


class TeamMembershipSanity(Base):
    def test_the_fixture_is_what_the_tests_assume(self):
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=self.member).exists())
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.member).exists()
        )

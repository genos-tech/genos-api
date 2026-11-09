"""A task comment written outside the socket handler is announced (MIP-1348).

The app announces a comment typed in the app (genos-sockets' `task_comment`
handler). The agent's and MCP's comment tools, undo, and plain REST clients
wrote the row and told no one, so a colleague's open task did not show it until
reopened (measured: 0 broadcast events). After such a write commits, the API now
posts the change to genos-sockets, which sends it to the project room.

These pin what is sent, when (after commit only), that it is signed with the
right secret, that a failure never costs the write, and that every writer is
covered: the REST view's POST / PUT / DELETE and the three agent tools.
"""

import hashlib
import hmac
import json
from unittest import mock

import requests
from django.db import transaction
from django.test import override_settings

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.delete_comment import DELETE_COMMENT
from origin.search_engine.agent.tools.update_comment import UPDATE_COMMENT
from origin.services import realtime_push

from .test_base import BaseAPITestCase

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
URL = f"{SOCKETS}/internal/broadcast/task-comment"
REST = "/api/v2/task/comment/"


def _body(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class RealtimePushTests(BaseAPITestCase):
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
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    # The test case wraps each test in a transaction that never commits, so run
    # the on_commit callbacks explicitly, as a real commit would.
    def _tool(self, tool, **args):
        with self.captureOnCommitCallbacks(execute=True):
            return tool.run({"task_id": self.task.task_id, **args}, self.ctx)

    def _comment(self, text="hello"):
        return TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=_body(text)
        )

    def _sent(self):
        """The JSON body of each push, in order."""
        return [json.loads(c.kwargs["data"]) for c in self.post.call_args_list]

    # ---- what is sent ----

    def test_a_new_comment_pushes_the_facts_the_room_needs(self):
        self._tool(ADD_COMMENT, body_text="hi there")

        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], URL)
        self.assertEqual(
            self._sent()[0],
            {
                "projectId": self.project.project_id,
                "taskId": self.task.task_id,
                "commentId": 1,
                "action": "created",
                "isPrivate": False,
            },
        )

    def test_a_private_project_is_flagged_private(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_private=True)
        self._tool(ADD_COMMENT, body_text="hi")
        self.assertTrue(self._sent()[0]["isPrivate"])

    def test_no_comment_text_leaves_the_api(self):
        self._tool(ADD_COMMENT, body_text="a very private remark")
        self.assertNotIn("private remark", self.post.call_args.kwargs["data"].decode())

    def test_the_push_is_signed_with_the_sockets_secret_over_timestamp_and_body(self):
        self._tool(ADD_COMMENT, body_text="hi")

        headers = self.post.call_args.kwargs["headers"]
        body = self.post.call_args.kwargs["data"]
        stamp = headers["X-Genos-Timestamp"]
        expected = hmac.new(
            SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(headers["X-Genos-Signature"], "sha256=" + expected)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertLessEqual(self.post.call_args.kwargs["timeout"], 5)
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])

    # ---- every writer ----

    def test_the_rest_post_pushes(self):
        self.authenticate()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                REST, {"task_id": self.task.task_id, "comment_body": _body("hi")}, format="json"
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual([p["action"] for p in self._sent()], ["created"])

    def test_the_rest_put_pushes_an_edit(self):
        self._comment()
        self.authenticate()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.put(
                REST,
                {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("v2")},
                format="json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([p["action"] for p in self._sent()], ["edited"])

    def test_the_rest_delete_pushes_the_live_count(self):
        self._comment()
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=2, comment_body=_body("two")
        )
        self.authenticate()
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.delete(f"{REST}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(resp.status_code, 200)
        sent = self._sent()
        self.assertEqual([p["action"] for p in sent], ["deleted"])
        self.assertEqual(sent[0]["commentId"], 1)
        # Counted AFTER the delete: the app sets its chip from this.
        self.assertEqual(sent[0]["taskCommentCount"], 1)

    def test_the_agents_edit_pushes(self):
        self._comment()
        self._tool(UPDATE_COMMENT, comment_id=1, body_text="rewritten")
        self.assertEqual([p["action"] for p in self._sent()], ["edited"])

    def test_the_agents_delete_pushes(self):
        self._comment()
        self._tool(DELETE_COMMENT, comment_id=1)
        sent = self._sent()
        self.assertEqual([p["action"] for p in sent], ["deleted"])
        self.assertEqual(sent[0]["taskCommentCount"], 0)

    def test_deleting_an_already_deleted_comment_pushes_nothing_more(self):
        self._comment()
        self._tool(DELETE_COMMENT, comment_id=1)
        self.post.reset_mock()
        self._tool(DELETE_COMMENT, comment_id=1)
        self.post.assert_not_called()

    # ---- when it is sent ----

    def test_nothing_is_sent_until_the_write_commits(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            ADD_COMMENT.run({"task_id": self.task.task_id, "body_text": "hi"}, self.ctx)
        self.post.assert_not_called()
        self.assertTrue(callbacks)

    def test_a_rolled_back_write_never_pushes(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    realtime_push.push_task_comment(self.task.task_id, 1, realtime_push.CREATED)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.post.assert_not_called()

    # ---- it never costs the write ----

    def test_an_unreachable_sockets_server_does_not_fail_the_comment(self):
        self.post.side_effect = requests.ConnectionError("down")
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            result = self._tool(ADD_COMMENT, body_text="hi")
        self.assertEqual(result["comment_id"], 1)
        self.assertTrue(TaskComments.objects.filter(task=self.task, comment_id=1).exists())
        self.assertIn("push failed", logs.output[0])

    def test_a_refusal_is_logged_not_raised(self):
        self.post.return_value = mock.Mock(status_code=401)
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            result = self._tool(ADD_COMMENT, body_text="hi")
        self.assertEqual(result["comment_id"], 1)
        self.assertIn("refused", logs.output[0])
        self.assertIn("status=401", logs.output[0])

    def test_an_unexpected_error_in_the_push_does_not_reach_the_caller(self):
        self.post.side_effect = ValueError("boom")
        # `on_commit(..., robust=True)`: Django logs the error and carries on. The
        # test helper reports it on "django.test"; a real commit, on
        # "django.db.backends.base".
        with self.assertLogs("django.test", level="ERROR"):
            result = self._tool(ADD_COMMENT, body_text="hi")
        self.assertEqual(result["comment_id"], 1)

    # ---- not configured ----

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_without_a_sockets_url_nothing_is_sent(self):
        result = self._tool(ADD_COMMENT, body_text="hi")
        self.assertEqual(result["comment_id"], 1)
        self.post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_without_a_secret_nothing_is_sent_and_it_is_logged(self):
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            self._tool(ADD_COMMENT, body_text="hi")
        self.post.assert_not_called()
        self.assertIn("SOCKETS_INTERNAL_SECRET is unset", logs.output[0])

    def test_a_task_without_a_project_has_no_room_to_announce_to(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(project=None)
        realtime_push.push_task_comment(self.task.task_id, 1, realtime_push.CREATED)
        self.post.assert_not_called()

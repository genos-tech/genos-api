"""Comments left ON A TASK BODY, over MCP (MIP-1863).

A task body is a Yjs document (`task-body:<id>`) with the same `threads` map a note has, so its
comment threads are read and written through genos-collab's internal endpoints, as a note's are.
Collab is stubbed at `requests.post` here; the answers are shaped as collab's `readThreads` /
`handleCreateThread` / reply / resolve answer them (genos-collab threadsRead.js, threadsCreate.js,
threadsWrite.js). What is pinned:

- `get_task` carries the body's threads as `body_comment_threads` (open first, resolved left out
  unless `include_resolved`), `[]` for a body nobody commented on, `null` with a sentence when
  collab cannot answer, and the body itself is still returned;
- the three write tools (`comment_on_task_body`, `reply_to_task_body_comment`,
  `resolve_task_body_comment`) act only for someone who may read the task (`load_task`), and collab
  is never asked otherwise;
- resolve: the thread's author, the task's assignee or reporter may resolve (`mayResolveAny`);
  another project member may not, and collab, which knows the author, has the last word;
- the reply's `via` marker is the reduced one, and a body nobody opened is refused with a sentence
  that says what to do, creating nothing;
- the tools are MCP-only: never in the in-app registry the model sees.
"""

from __future__ import annotations

import json
from unittest import mock

import requests
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import collab_push
from origin.tests.test_mcp_endpoint import McpBase

COLLAB = "http://collab.internal:8891"
COLLAB_SECRET = "test-only-collab-internal-secret"
THREAD = "6c1c0a52-2a36-4a2f-93cb-0d1a1b6f8c11"
RESOLVED = "7d2d1b63-3b3b-4b3b-92dc-1c2d3e4f5a6b"
ANCHOR = "Retention is 180 days"


def _body(text):
    return [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": text, "styles": {}}],
            "children": [],
        }
    ]


def _comment(user_id, text, at="2026-11-20T10:00:00.000Z"):
    return {
        "id": f"c-{abs(hash((user_id, text))) % 10**6}",
        "userId": user_id,
        "createdAt": at,
        "updatedAt": at,
        "deleted": False,
        "body": _body(text),
    }


@override_settings(COLLAB_INTERNAL_URL=COLLAB, COLLAB_INTERNAL_SECRET=COLLAB_SECRET)
class TaskBodyBase(McpBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, body, status=200):
        self.post.return_value = mock.Mock(status_code=status, json=mock.Mock(return_value=body))

    def call(self, name, args):
        return self._tool_text(self._call(name, args))

    def ok(self, name, args):
        text, is_error = self.call(name, args)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, name, args):
        text, is_error = self.call(name, args)
        self.assertTrue(is_error, text)
        return text

    @property
    def sent(self):
        return self.post.call_args.kwargs["json"]


class GetTaskBodyThreadsTests(TaskBodyBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.answer(
            {
                "threads": [
                    {
                        "id": THREAD,
                        "resolved": False,
                        "resolvedBy": None,
                        "resolvedAt": None,
                        "createdAt": "2026-11-20T10:00:00.000Z",
                        "anchorText": ANCHOR,
                        "comments": [
                            _comment(str(self.user.id), "why 180?"),
                            _comment(str(self.user.id), "the audit needs it"),
                        ],
                    },
                    {
                        "id": RESOLVED,
                        "resolved": True,
                        "resolvedBy": str(self.user.id),
                        "resolvedAt": "2026-11-20T11:00:00.000Z",
                        "createdAt": "2026-11-19T10:00:00.000Z",
                        "anchorText": "Phase 1 ships first",
                        "comments": [
                            _comment(str(self.user.id), "done?", "2026-11-19T10:00:00.000Z")
                        ],
                    },
                ]
            }
        )

    def test_get_task_lists_the_open_threads_on_the_body_and_says_what_it_left_out(self):
        out = self.ok("get_task", {"task_id": self.task.task_id})

        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/read-threads")
        self.assertEqual(self.sent, {"documentName": f"task-body:{self.task.task_id}"})
        self.assertEqual(out["task_id"], self.task.task_id)
        threads = out["body_comment_threads"]
        self.assertEqual([t["thread_id"] for t in threads], [THREAD])
        self.assertIn(ANCHOR, threads[0]["anchor_text"])
        self.assertFalse(threads[0]["resolved"])
        self.assertEqual(len(threads[0]["comments"]), 2)
        self.assertIn("why 180?", json.dumps(threads[0]["comments"][0]))
        self.assertIn("1 resolved thread not shown", out["body_comment_threads_note"])
        self.assertIn("include_resolved", out["body_comment_threads_note"])

    def test_include_resolved_brings_the_settled_threads_too(self):
        out = self.ok("get_task", {"task_id": self.task.task_id, "include_resolved": True})

        self.assertEqual([t["thread_id"] for t in out["body_comment_threads"]], [THREAD, RESOLVED])
        resolved = out["body_comment_threads"][1]
        self.assertTrue(resolved["resolved"])
        self.assertNotIn("body_comment_threads_note", out)

    def test_a_body_nobody_commented_on_has_an_empty_list_not_a_missing_key(self):
        self.answer({"threads": []})
        out = self.ok("get_task", {"task_id": self.task.task_id})

        self.assertEqual(out["body_comment_threads"], [])
        self.assertNotIn("body_comment_threads_note", out)

    def test_collab_that_cannot_answer_gives_null_and_the_body_is_still_there(self):
        self.post.side_effect = requests.ConnectionError("collab is down")
        out = self.ok("get_task", {"task_id": self.task.task_id})

        self.assertIsNone(out["body_comment_threads"])
        self.assertIn("could not be read", out["body_comment_threads_note"])
        self.assertEqual(out["task_id"], self.task.task_id)
        self.assertEqual(out["title"], self.task.title)

    def test_a_task_the_caller_cannot_read_is_refused_before_collab_is_asked(self):
        outsider = self.user2
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=outsider).exists()
        )
        self._key(scope=SCOPE_WRITE, user=outsider)
        text, is_error = self.call("get_task", {"task_id": self.task.task_id})

        self.assertTrue(is_error, text)
        self.post.assert_not_called()


class TaskBodyWriteBase(TaskBodyBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.answer({"ok": True, "commentId": "c-9", "commentCount": 2, "threadResolved": False})
        # A task the caller is only a project member of: neither its reporter nor its assignee.
        self.colleague = self.user2
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.colleague)
        self.theirs = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.colleague,
            assignee=self.colleague,
            title="Their task",
            status="Open",
            project_task_number=8,
        )


class ReplyToTaskBodyCommentTests(TaskBodyWriteBase):
    def test_a_reply_asks_collab_for_this_tasks_body_as_the_caller(self):
        out = self.ok(
            "reply_to_task_body_comment",
            {"task_id": self.task.task_id, "thread_id": THREAD, "text": "Six months, per audit."},
        )

        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/reply-thread")
        self.assertEqual(
            self.sent,
            {
                "documentName": f"task-body:{self.task.task_id}",
                "threadId": THREAD,
                "userId": str(self.user.id),
                "text": "Six months, per audit.",
                "metadata": {"via": {"kind": "api_key"}},
            },
        )
        self.assertEqual(out["thread_id"], THREAD)
        self.assertEqual(out["comment_id"], "c-9")
        self.assertEqual(out["task_id"], self.task.task_id)

    def test_a_display_id_names_the_task(self):
        self.ok(
            "reply_to_task_body_comment",
            {"task_id": self.task.display_id, "thread_id": THREAD, "text": "hi"},
        )
        self.assertEqual(self.sent["documentName"], f"task-body:{self.task.task_id}")

    def test_someone_who_cannot_read_the_task_cannot_reply_and_collab_is_not_asked(self):
        self._key(scope=SCOPE_WRITE, user=self.user2)
        other = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Private",
            status="Open",
            project_task_number=9,
        )
        # user2 is on the team but not on this project: not a reader of the task.
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        text = self.refused(
            "reply_to_task_body_comment",
            {"task_id": other.task_id, "thread_id": THREAD, "text": "hi"},
        )
        self.assertIn("Not authorized", text)
        self.post.assert_not_called()

    def test_a_body_nobody_opened_is_refused_with_what_to_do(self):
        self.post.return_value = mock.Mock(
            status_code=404,
            json=mock.Mock(return_value={"error": "Not found.", "code": "no_document"}),
        )
        text = self.refused(
            "reply_to_task_body_comment",
            {"task_id": self.task.task_id, "thread_id": THREAD, "text": "hi"},
        )
        self.assertIn("Open the task", text)

    def test_no_answer_from_collab_says_the_reply_may_have_been_added(self):
        self.post.side_effect = requests.ReadTimeout("slow")
        text = self.refused(
            "reply_to_task_body_comment",
            {"task_id": self.task.task_id, "thread_id": THREAD, "text": "hi"},
        )
        self.assertIn("may or may not have been added", text)
        self.assertIn("get_task", text)

    def test_a_thread_collab_does_not_have_is_said_so(self):
        self.post.return_value = mock.Mock(
            status_code=404, json=mock.Mock(return_value={"error": "thread not found"})
        )
        text = self.refused(
            "reply_to_task_body_comment",
            {"task_id": self.task.task_id, "thread_id": THREAD, "text": "hi"},
        )
        self.assertIn("was not found on this task", text)
        self.assertIn("get_task", text)


class ResolveTaskBodyCommentTests(TaskBodyWriteBase):
    def test_the_assignee_or_reporter_may_resolve_any_thread(self):
        self.answer(
            {
                "ok": True,
                "resolved": True,
                "alreadyResolved": False,
                "resolvedBy": str(self.user.id),
            }
        )
        out = self.ok(
            "resolve_task_body_comment", {"task_id": self.task.task_id, "thread_id": THREAD}
        )

        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/resolve-thread")
        self.assertEqual(self.sent["documentName"], f"task-body:{self.task.task_id}")
        self.assertTrue(self.sent["mayResolveAny"])
        self.assertEqual(self.sent["userId"], str(self.user.id))
        self.assertEqual(out["thread_id"], THREAD)

    def test_another_project_member_may_only_resolve_their_own_thread(self):
        self.answer(
            {
                "ok": True,
                "resolved": True,
                "alreadyResolved": False,
                "resolvedBy": str(self.user.id),
            }
        )
        self.ok("resolve_task_body_comment", {"task_id": self.theirs.task_id, "thread_id": THREAD})

        self.assertFalse(self.sent["mayResolveAny"])

    def test_collab_refusing_someone_elses_thread_reads_clearly(self):
        self.post.return_value = mock.Mock(
            status_code=403, json=mock.Mock(return_value={"error": "forbidden"})
        )
        text = self.refused(
            "resolve_task_body_comment", {"task_id": self.theirs.task_id, "thread_id": THREAD}
        )
        self.assertIn("reply_to_task_body_comment", text)


class CommentOnTaskBodyTests(TaskBodyWriteBase):
    def test_a_new_thread_on_a_passage_of_the_body(self):
        self.answer(
            {
                "ok": True,
                "threadId": THREAD,
                "commentId": "c-1",
                "blockId": "b1",
                "occurrence": 1,
                "matches": 1,
            }
        )
        out = self.ok(
            "comment_on_task_body",
            {"task_id": self.task.task_id, "anchor_text": ANCHOR, "text": "why 180?"},
        )

        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/create-thread")
        self.assertEqual(
            self.sent,
            {
                "documentName": f"task-body:{self.task.task_id}",
                "userId": str(self.user.id),
                "text": "why 180?",
                "anchorText": ANCHOR,
                "metadata": {"via": {"kind": "api_key"}},
            },
        )
        self.assertEqual(out["thread_id"], THREAD)
        self.assertEqual(out["block_id"], "b1")

    def test_a_passage_that_is_not_in_the_body_writes_nothing(self):
        self.post.return_value = mock.Mock(
            status_code=404,
            json=mock.Mock(return_value={"error": "anchor not found", "code": "anchor_not_found"}),
        )
        text = self.refused(
            "comment_on_task_body",
            {"task_id": self.task.task_id, "anchor_text": "not in the body", "text": "hi"},
        )
        self.assertIn("was not found in the task body", text)
        self.assertIn("Nothing was changed", text)

    def test_a_passage_found_twice_asks_for_a_block(self):
        self.post.return_value = mock.Mock(
            status_code=409,
            json=mock.Mock(
                return_value={
                    "error": "anchor ambiguous",
                    "code": "anchor_ambiguous",
                    "matches": 2,
                    "blockIds": ["b1", "b2"],
                    "blockCount": 2,
                }
            ),
        )
        text = self.refused(
            "comment_on_task_body",
            {"task_id": self.task.task_id, "anchor_text": ANCHOR, "text": "hi"},
        )
        self.assertIn("block_id", text)
        self.assertIn("Nothing was changed", text)


class TaskBodyToolsAreMcpOnlyTests(TaskBodyBase):
    def test_the_three_tools_are_mcp_only_and_the_read_stays_in_get_task(self):
        for name in (
            "comment_on_task_body",
            "reply_to_task_body_comment",
            "resolve_task_body_comment",
        ):
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertNotIn(name, REGISTRY)
        # The in-app agent's fetch_task is untouched: no body threads in what the model sees.
        self.assertNotIn("body_comment_threads", REGISTRY["fetch_task"].description)

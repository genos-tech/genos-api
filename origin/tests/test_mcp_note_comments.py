"""`get_note` over MCP returns a note's comment threads (MIP-1064).

A BlockNote comment lives in the note's Yjs doc, which genos-collab owns, so
`get_note` (which read only the `body` column) never showed a reviewer's comments
to Claude. The MCP layer now asks collab for the threads after `fetch_note` has
passed its access check. The in-app agent shares `fetch_note`, so it must see none
of it: that is pinned here too, because a change in what it returns is a
model-visible change that waits for the December eval.
"""

from __future__ import annotations

import json
from unittest import mock

import jwt
import requests
from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.services import collab_push
from origin.services.note_comments import MAX_COMMENT_CHARS, MAX_THREADS
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools as mcp_tools

User = get_user_model()

COLLAB = "http://collab.internal:8891"
COLLAB_SECRET = "test-only-collab-internal-secret"


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


def _comment(user_id, text, *, ts="2026-11-06T09:00:00.000Z", edited=False, deleted=False):
    return {
        "id": f"c-{abs(hash((user_id, text))) % 10**6}",
        "userId": user_id,
        "createdAt": ts,
        "updatedAt": "2026-11-06T09:30:00.000Z" if edited else ts,
        "deleted": deleted,
        "body": None if deleted else _blocks(text),
    }


def _thread(
    thread_id, comments, *, anchor="Retention is 180 days", resolved=False, resolved_by=None
):
    return {
        "id": thread_id,
        "resolved": resolved,
        "resolvedBy": resolved_by,
        "resolvedAt": "2026-11-06T10:00:00.000Z" if resolved else None,
        "createdAt": "2026-11-06T09:00:00.000Z",
        "anchorText": anchor,
        "comments": comments,
    }


@override_settings(COLLAB_INTERNAL_URL=COLLAB, COLLAB_INTERNAL_SECRET=COLLAB_SECRET)
class TestGetNoteComments(McpBase):
    def setUp(self):
        super().setUp()
        self.note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="Plan",
            body=_blocks("The plan body."),
        )
        self.url = (
            "https://genosai.dev/workspace/notes/task/project/"
            f"{self.project.project_id}/task/{self.task.task_id}/note/{self.note.note_id}"
        )
        self.uid = str(self.user.id)
        self.threads = [
            _thread(
                "t-open",
                [_comment(self.uid, "why 180 days?"), _comment(self.uid, "audit", edited=True)],
            ),
            _thread(
                "t-done",
                [_comment(self.uid, "typo")],
                anchor="Phase 1",
                resolved=True,
                resolved_by=self.uid,
            ),
        ]
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self._collab_answers({"threads": self.threads})
        self.addCleanup(patcher.stop)
        self._key(scope=SCOPE_READ)

    def _collab_answers(self, body, status=200):
        self.post.return_value = mock.Mock(status_code=status, json=mock.Mock(return_value=body))

    def _get(self, **extra):
        text, is_error = self._tool_text(self._call("get_note", {"note_id": self.url, **extra}))
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    # -- what comes back --

    def test_open_threads_come_back_with_anchor_author_and_replies(self):
        out = self._get()

        self.assertEqual([t["thread_id"] for t in out["comment_threads"]], ["t-open"])
        thread = out["comment_threads"][0]
        self.assertEqual(thread["anchor_text"], "Retention is 180 days")
        self.assertFalse(thread["resolved"])
        first, reply = thread["comments"]
        self.assertEqual(first["author"], {"user_id": self.uid, "username": "testuser"})
        self.assertEqual(first["markdown"], "why 180 days?")
        self.assertEqual(first["ts"], "2026-11-06T09:00:00.000Z")
        self.assertNotIn("edited", first)
        self.assertIs(reply["edited"], True)
        self.assertIn("The plan body.", out["body_markdown"], "the body is still there")

    def test_resolved_threads_are_counted_but_left_out_unless_asked_for(self):
        out = self._get()
        self.assertIn("1 resolved thread not shown", out["comment_threads_note"])
        self.assertIn("include_resolved", out["comment_threads_note"])

        out = self._get(include_resolved=True)
        self.assertEqual([t["thread_id"] for t in out["comment_threads"]], ["t-open", "t-done"])
        done = out["comment_threads"][1]
        self.assertTrue(done["resolved"])
        self.assertEqual(done["resolved_by"], {"user_id": self.uid, "username": "testuser"})
        self.assertNotIn("comment_threads_note", out)

    def test_include_resolved_accepts_a_string_a_model_might_send_and_nothing_else(self):
        self.assertEqual(len(self._get(include_resolved="true")["comment_threads"]), 2)
        self.assertEqual(len(self._get(include_resolved="yes")["comment_threads"]), 1)

    def test_open_threads_are_listed_before_resolved_ones_whatever_collab_sent(self):
        self._collab_answers({"threads": list(reversed(self.threads))})
        out = self._get(include_resolved=True)
        self.assertEqual([t["thread_id"] for t in out["comment_threads"]], ["t-open", "t-done"])

    def test_a_deleted_comment_has_no_text_and_an_unknown_author_has_no_name(self):
        stranger = "11111111-1111-4111-8111-111111111111"
        self._collab_answers(
            {
                "threads": [
                    _thread(
                        "t", [_comment(self.uid, "gone", deleted=True), _comment(stranger, "hello")]
                    )
                ]
            }
        )
        first, second = self._get()["comment_threads"][0]["comments"]
        self.assertTrue(first["deleted"])
        self.assertNotIn("markdown", first)
        self.assertEqual(second["author"], {"user_id": stranger, "username": None})
        self.assertEqual(second["markdown"], "hello")

    def test_a_thread_whose_text_was_deleted_has_no_anchor(self):
        self._collab_answers({"threads": [_thread("t", [_comment(self.uid, "x")], anchor=None)]})
        self.assertIsNone(self._get()["comment_threads"][0]["anchor_text"])

    def test_a_note_with_no_comments_says_so_with_an_empty_list(self):
        self._collab_answers({"threads": []})
        out = self._get()
        self.assertEqual(out["comment_threads"], [])
        self.assertNotIn("comment_threads_note", out)

    def test_a_mention_in_a_comment_keeps_its_chip(self):
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "ping ", "styles": {}},
                    {"type": "mention", "props": {"userId": self.uid, "userName": "testuser"}},
                ],
            }
        ]
        comment = {**_comment(self.uid, "x"), "body": body}
        self._collab_answers({"threads": [_thread("t", [comment])]})
        self.assertIn(
            f"(user:{self.uid})", self._get()["comment_threads"][0]["comments"][0]["markdown"]
        )

    def test_threads_and_long_comments_are_capped(self):
        many = [
            _thread(f"t{i}", [_comment(self.uid, "x" * (MAX_COMMENT_CHARS + 500))])
            for i in range(MAX_THREADS + 5)
        ]
        self._collab_answers({"threads": many})
        out = self._get()
        self.assertEqual(len(out["comment_threads"]), MAX_THREADS)
        self.assertIn(f"first {MAX_THREADS} threads", out["comment_threads_note"])
        markdown = out["comment_threads"][0]["comments"][0]["markdown"]
        self.assertEqual(len(markdown), MAX_COMMENT_CHARS + 1)
        self.assertTrue(markdown.endswith("…"))

    # -- what is asked of collab --

    def test_collab_is_asked_for_this_notes_doc_with_the_internal_token(self):
        self._get()

        self.post.assert_called_once()
        call = self.post.call_args
        self.assertEqual(call.args[0], f"{COLLAB}/internal/documents/read-threads")
        self.assertEqual(call.kwargs["json"], {"documentName": f"task-note:{self.note.note_id}"})
        self.assertFalse(call.kwargs["allow_redirects"])
        token = call.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token, COLLAB_SECRET, algorithms=["HS256"], audience="genos-collab-internal"
        )
        self.assertEqual(claims["aud"], "genos-collab-internal")

    def test_a_personal_note_reads_its_my_note_doc(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=_blocks("x")
        )
        self._tool_text(
            self._call("get_note", {"note_id": str(note.note_id), "note_type": "personal"})
        )
        self.assertEqual(
            self.post.call_args.kwargs["json"], {"documentName": f"my-note:{note.note_id}"}
        )

    # -- when comments can't be read the body still comes back, and it says so --

    def _assert_unavailable(self):
        out = self._get()
        self.assertIsNone(
            out["comment_threads"], "null, not [], so no comments and unreadable differ"
        )
        self.assertIn("could not be read", out["comment_threads_note"])
        self.assertIn("The plan body.", out["body_markdown"])

    def test_collab_unreachable(self):
        self.post.side_effect = requests.ConnectionError("down")
        self._assert_unavailable()

    def test_collab_refuses(self):
        self._collab_answers({"error": "invalid token"}, status=401)
        self._assert_unavailable()
        # The status decides, not the body: a refusal that still carries threads is a refusal.
        self._collab_answers({"threads": self.threads}, status=401)
        self._assert_unavailable()

    def test_collab_answers_nonsense(self):
        self._collab_answers({"threads": "nope"})
        self._assert_unavailable()
        self.post.return_value = mock.Mock(
            status_code=200, json=mock.Mock(side_effect=ValueError("not json"))
        )
        self._assert_unavailable()

    @override_settings(COLLAB_INTERNAL_URL="")
    def test_collab_not_configured(self):
        self._assert_unavailable()
        self.post.assert_not_called()

    # -- access --

    def test_a_note_the_caller_cannot_read_is_refused_and_collab_is_never_asked(self):
        private = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Private", body=_blocks("secret")
        )
        self._key(scope=SCOPE_READ, user=self.user2)

        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": str(private.note_id), "note_type": "personal"})
        )

        self.assertTrue(is_error)
        self.assertNotIn("why 180", text)
        self.post.assert_not_called()

    def test_a_missing_note_is_an_error_and_collab_is_never_asked(self):
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": "999999", "note_type": "task"})
        )
        self.assertTrue(is_error, text)
        self.post.assert_not_called()

    # -- the schema --

    def test_include_resolved_is_published_and_a_typo_is_still_refused(self):
        schema = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}["get_note"][
            "inputSchema"
        ]
        self.assertEqual(schema["properties"]["include_resolved"]["type"], "boolean")

        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": self.url, "include_resolve": True})
        )
        self.assertTrue(is_error)
        self.assertIn("include_resolved", text)
        self.post.assert_not_called()

    # -- the in-app agent sees none of it --

    def test_the_in_app_note_tool_is_unchanged(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=self.uid)
        payload = REGISTRY["fetch_note"].run(
            {"note_type": "task", "note_id": self.note.note_id}, ctx
        )

        self.assertNotIn("comment_threads", payload)
        self.assertNotIn("comment_threads_note", payload)
        self.post.assert_not_called()
        declared = {d.name: d for d in _build_tool_declarations(set())}["fetch_note"]
        self.assertEqual(set(declared.parameters_schema["properties"]), {"note_type", "note_id"})
        self.assertNotIn("comment_threads", declared.description)
        self.assertIn("include_resolved", mcp_tools.BY_NAME["get_note"].mcp_only_args)

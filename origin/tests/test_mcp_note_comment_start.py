"""`comment_on_note`: start a comment thread on a passage of a note, over MCP (MIP-1644).

A comment lives in the note's Yjs doc, and a new thread also needs a `comment` mark on the words it is
about, so the tool asks genos-collab's `create-thread` and collab does both in one transaction. Collab is
stubbed here at `requests.post` (its own side is tested in genos-collab `threadsCreate.test.js`). What is
pinned:

- the request: exactly what collab is asked, as the caller, with the reduced `via` and only the optional
  fields that were given; a block link as `note_id` supplies the block;
- every way collab can say no reads as an instruction a model can act on, writes nothing, and never
  echoes the passage: not found (and found only in a code block), ambiguous (how many, where, what to
  pass), an unknown block, an occurrence past the last, a note with no live document, an older collab
  that answers "200 OK" to anything, a timeout after the request went out;
- the arguments are checked before collab is asked: the passage and the comment, the block id, the
  occurrence;
- access: whoever can read a note may comment on it (viewers included), and collab is never asked for a
  note the caller cannot see;
- the published schema lets a client send the passage and the comment with a link alone, and the tool is
  MCP-only: never in the in-app registry or its declarations.
"""

from __future__ import annotations

import json

import requests
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.note.common_note_models import NotePermissionMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import collab_push
from origin.views.public.mcp import tools as mcp_tools

from .test_mcp_note_thread_writes import (
    COLLAB,
    EDITOR_ROLE,
    VIEWER_ROLE,
    NoteThreadWriteBase,
)

COMMENT = "comment_on_note"
THREAD = "6c1c0a52-2a36-4a2f-93cb-0d1a1b6f8c11"
CREATED = {
    "ok": True,
    "threadId": THREAD,
    "commentId": "c-1",
    "blockId": "b-intro",
    "occurrence": 1,
    "matches": 1,
}


class StartBase(NoteThreadWriteBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.answer(CREATED)

    def start(self, **extra):
        return self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "anchor_text": "180 days",
                    "text": "Why 180?",
                    **extra,
                },
            )
        )

    def started(self, **extra):
        text, is_error = self.start(**extra)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused_with(self, **extra):
        text, is_error = self.start(**extra)
        self.assertTrue(is_error, text)
        return text

    def link(self, query=""):
        return f"https://app.genosai.dev/workspace/notes/my/{self.note.note_id}{query}"


class RequestTests(StartBase):
    def test_collab_is_asked_for_exactly_this_thread_as_the_caller(self):
        out = self.started()
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/create-thread")
        self.assertEqual(
            self.sent,
            {
                "documentName": f"my-note:{self.note.note_id}",
                "userId": str(self.user.id),
                "text": "Why 180?",
                "anchorText": "180 days",
                "metadata": {"via": {"kind": "api_key"}},
            },
        )
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(out["thread_id"], THREAD)
        self.assertEqual(out["comment_id"], "c-1")
        self.assertEqual(out["block_id"], "b-intro")
        self.assertEqual(out["matches"], 1)
        self.assertEqual(out["note_id"], self.note.note_id)
        self.assertEqual(out["note_type"], "personal")

    def test_the_optional_fields_are_sent_only_when_given(self):
        self.started(block_id="b-intro", occurrence=2)
        self.assertEqual((self.sent["blockId"], self.sent["occurrence"]), ("b-intro", 2))
        self.post.reset_mock()
        self.started()
        self.assertNotIn("blockId", self.sent)
        self.assertNotIn("occurrence", self.sent)

    def test_the_passage_and_the_comment_are_trimmed_but_otherwise_sent_as_written(self):
        self.started(anchor_text="  Retention  is 180 days \n", text="\n Why?\nAnd then?  ")
        self.assertEqual(self.sent["anchorText"], "Retention  is 180 days")
        self.assertEqual(self.sent["text"], "Why?\nAnd then?")

    def test_a_block_link_as_the_note_names_the_note_and_the_block(self):
        text, is_error = self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_id": self.link("?block=b-intro"),
                    "anchor_text": "180 days",
                    "text": "Why 180?",
                },
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual(self.sent["blockId"], "b-intro")

    def test_a_link_and_a_block_id_that_disagree_are_refused_before_collab(self):
        text, is_error = self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_id": self.link("?block=b-intro"),
                    "block_id": "b-other",
                    "anchor_text": "180 days",
                    "text": "x",
                },
            )
        )
        self.assertTrue(is_error)
        self.assertIn("agree", text)
        self.post.assert_not_called()

    def test_a_plain_note_link_with_no_block_searches_the_whole_note(self):
        text, is_error = self._tool_text(
            self._call(COMMENT, {"note_id": self.link(), "anchor_text": "180 days", "text": "x"})
        )
        self.assertFalse(is_error, text)
        self.assertNotIn("blockId", self.sent)


class CollabSaysNoTests(StartBase):
    def collab_refuses(self, status, code, **facts):
        self.answer({"error": "refused", "code": code, **facts}, status)

    def test_a_passage_that_is_not_there(self):
        self.collab_refuses(404, "anchor_not_found", blockedBlocks=0)
        text = self.refused_with()
        self.assertIn("not found in the note", text)
        self.assertIn("exactly as written", text)
        self.assertIn("@mention", text)
        self.assertIn("Nothing was changed", text)
        self.assertNotIn("code, math or diagram block", text)

    def test_a_passage_that_is_only_in_a_code_block_says_so(self):
        self.collab_refuses(404, "anchor_not_found", blockedBlocks=1)
        self.assertIn("code, math or diagram block", self.refused_with())

    def test_a_passage_found_more_than_once_says_how_often_where_and_what_to_pass(self):
        self.collab_refuses(409, "anchor_ambiguous", matches=3, blockIds=["b1", "b2"], blockCount=2)
        text = self.refused_with()
        self.assertIn("3 times", text)
        self.assertIn("2 blocks (b1, b2)", text)
        self.assertIn("`block_id`", text)
        self.assertIn("`occurrence`", text)
        self.assertIn("Nothing was changed", text)

    def test_a_long_list_of_blocks_is_marked_as_cut(self):
        self.collab_refuses(
            409, "anchor_ambiguous", matches=14, blockIds=["b1", "b2"], blockCount=12
        )
        self.assertIn("12 blocks (b1, b2, ...)", self.refused_with())

    def test_an_unknown_block(self):
        self.collab_refuses(404, "block_not_found")
        text = self.refused_with(block_id="b-gone")
        self.assertIn("block was not found", text)
        self.assertIn("include_blocks", text)

    def test_an_occurrence_past_the_last(self):
        self.collab_refuses(400, "occurrence_out_of_range", matches=2)
        text = self.refused_with(occurrence=5)
        self.assertIn("appears 2 time(s)", text)

    def test_a_note_nobody_opened_in_the_editor(self):
        self.collab_refuses(404, "no_document")
        text = self.refused_with()
        self.assertIn("Nobody has opened this note in the editor", text)
        self.assertIn("Open it once", text)

    def test_an_older_collab_that_answers_ok_to_anything_fails_safe(self):
        self.answer({"ok": "plain text OK"})
        text = self.refused_with()
        self.assertIn("not available on this server yet", text)

    def test_a_collab_that_is_down_says_nothing_was_changed(self):
        self.post.side_effect = requests.ConnectTimeout("nope")
        text = self.refused_with()
        self.assertIn("could not be reached", text)
        self.assertIn("Nothing was changed", text)

    def test_a_request_that_went_out_with_no_answer_is_not_retried_and_says_it_may_have_landed(
        self,
    ):
        self.post.side_effect = requests.ReadTimeout("slow")
        text = self.refused_with()
        self.assertIn("may or may not have been added", text)
        self.assertEqual(self.post.call_count, 1, "a second POST would be a second thread")

    def test_no_refusal_echoes_the_passage(self):
        secret_passage = "Salary of the CFO is 12345"
        for status, code, facts in (
            (404, "anchor_not_found", {"blockedBlocks": 1}),
            (409, "anchor_ambiguous", {"matches": 2, "blockIds": ["a"], "blockCount": 1}),
            (404, "block_not_found", {}),
        ):
            with self.subTest(code=code):
                self.collab_refuses(status, code, **facts)
                self.assertNotIn(secret_passage, self.refused_with(anchor_text=secret_passage))


class ArgumentTests(StartBase):
    def test_the_comment_is_required_and_bounded(self):
        for bad in ("", "   ", None):
            with self.subTest(text=bad):
                self.assertIn("`text` is required", self.refused_with(text=bad))
        self.assertIn("at most 4000", self.refused_with(text="x" * 4001))
        self.assertIn("more than 50 lines", self.refused_with(text="\n".join(["x"] * 51)))
        self.post.assert_not_called()

    def test_the_passage_is_required_and_bounded(self):
        for bad in ("", "  ", None):
            with self.subTest(anchor=bad):
                self.assertIn("`anchor_text` is required", self.refused_with(anchor_text=bad))
        self.assertIn("at most 500", self.refused_with(anchor_text="x" * 501))
        self.post.assert_not_called()

    def test_a_block_id_must_look_like_one(self):
        for bad in ("has space", "a/b", "x" * 65, 7):
            with self.subTest(block_id=bad):
                self.assertIn("`block_id`", self.refused_with(block_id=bad))
        self.post.assert_not_called()

    def test_an_occurrence_is_a_whole_number_from_one(self):
        for bad in (0, -1, 1.5, "abc", True, 1001):
            with self.subTest(occurrence=bad):
                self.assertIn("`occurrence`", self.refused_with(occurrence=bad))
        self.post.assert_not_called()

    def test_blank_optional_fields_are_no_field(self):
        self.started(block_id="", occurrence="")
        self.assertNotIn("blockId", self.sent)
        self.assertNotIn("occurrence", self.sent)


class AccessTests(StartBase):
    def test_a_viewer_may_comment_as_in_the_editor(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, VIEWER_ROLE)
        self._key(user=self.user2, scope=SCOPE_WRITE)
        self.started()
        self.assertEqual(self.sent["userId"], str(self.user2.id))

    def test_an_editor_may_comment(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, EDITOR_ROLE)
        self._key(user=self.user2, scope=SCOPE_WRITE)
        self.started()
        self.post.assert_called_once()

    def test_a_team_member_with_no_access_is_told_the_note_does_not_exist_and_collab_is_not_asked(
        self,
    ):
        self._key(user=self.user2, scope=SCOPE_WRITE)
        text = self.refused_with()
        self.assertIn("not found", text)
        self.post.assert_not_called()

    def test_a_read_only_key_cannot_even_see_the_tool(self):
        self._key(scope=SCOPE_READ)
        names = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertNotIn(COMMENT, names)


class PublishedTests(StartBase):
    def tool(self):
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        return tools[COMMENT]

    def test_a_client_may_send_the_passage_and_the_comment_with_a_link_alone(self):
        schema = self.tool()["inputSchema"]
        self.assertEqual(sorted(schema["required"]), ["anchor_text", "note_id", "text"])
        self.assertEqual(
            sorted(schema["properties"]),
            ["anchor_text", "block_id", "note_id", "note_type", "occurrence", "text"],
        )
        self.assertEqual(schema["properties"]["note_id"]["type"], "string")
        self.assertEqual(schema["properties"]["occurrence"]["type"], "integer")
        self.assertIn("limits the search", schema["properties"]["note_id"]["description"])

    def test_the_description_says_what_a_model_needs_to_get_it_right(self):
        text = self.tool()["description"]
        for needle in ("anchor_text", "exactly", "occurrence", "block_id", "reply_to_note_comment"):
            self.assertIn(needle, text)

    def test_the_reply_tool_no_longer_says_a_thread_cannot_be_started(self):
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertIn("comment_on_note", tools["reply_to_note_comment"]["description"])

    def test_it_is_a_write_that_adds_and_never_destroys(self):
        self.assertIn(COMMENT, mcp_tools.ADDITIVE_WRITE_TOOLS)
        tool = next(t for t in mcp_tools.CATALOG if t.name == COMMENT)
        self.assertTrue(tool.write)
        self.assertFalse(tool.destructive)

    def test_it_is_mcp_only(self):
        self.assertIn(COMMENT, MCP_ONLY_REGISTRY)
        self.assertNotIn(COMMENT, REGISTRY)
        self.assertNotIn(COMMENT, {d.name for d in _build_tool_declarations()})
        self.assertEqual(collab_push.CREATE_THREAD_PATH, "/internal/documents/create-thread")


@override_settings(COLLAB_INTERNAL_URL="", COLLAB_INTERNAL_SECRET="")
class NotConfiguredTests(StartBase):
    def test_a_server_with_no_collab_says_so_and_does_not_try(self):
        text = self.refused_with()
        self.assertIn("could not be reached", text)
        self.post.assert_not_called()

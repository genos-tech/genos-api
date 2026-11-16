"""`edit_note_block` takes a block link as its target (MIP-1655).

"Copy link to block" (MIP-146) gives a person `…/workspace/notes/my/12?block=<blockId>`. The tool
(MIP-1646) wanted `note_type` + `note_id` + `block_id`, so a client had to take the URL apart itself.
The note reference resolver already turned the URL into the note, but dropped the query. What is pinned:

- a link alone is a complete target: the note, its family and the block all come from it;
- a link and a `block_id` that name different blocks are refused, with nothing sent to collab, and a
  `block` that is not a block id (or is two different ones) is refused rather than ignored, so a stray
  `block_id` can never be the one that gets edited;
- a `?comment=` link names a thread, not a block, so it is never read as one;
- the link changes nothing else: a viewer is still refused, a block that is gone is still reported as
  gone, and every other note tool still ignores the query string;
- the published schema lets a client send the link alone, and only for this tool.
"""

from __future__ import annotations

from origin.search_engine.agent.tools import ToolError
from origin.views.public.mcp.resolve import block_in_note_link

from .test_mcp_note_block_edit import (
    EDIT,
    EDITOR_ROLE,
    VIEWER_ROLE,
    BlockBase,
    body,
    para,
)

HOST = "https://app.genosai.dev"


class LinkBase(BlockBase):
    def link(self, query="?block=b-para", host=HOST):
        return f"{host}/workspace/notes/my/{self.note.note_id}{query}"

    def edit_by_link(self, link, **extra):
        """Only what a person pasted, plus what the edit itself needs."""
        return self._tool_text(
            self._call(
                EDIT, {"note_id": link, "op": "replace", "content_text": "rewritten", **extra}
            )
        )

    def edited_ok(self, link, **extra):
        text, is_error = self.edit_by_link(link, **extra)
        self.assertFalse(is_error, text)
        return text

    def edited_refused(self, link, **extra):
        text, is_error = self.edit_by_link(link, **extra)
        self.assertTrue(is_error, text)
        return text


class LinkIsTheWholeTargetTests(LinkBase):
    def setUp(self):
        super().setUp()
        new = body()
        new[1] = para("rewritten", "b-para")
        self.collab_ok(new)

    def test_a_link_alone_names_the_note_its_family_and_the_block(self):
        self.edited_ok(self.link())
        self.post.assert_called_once()
        sent = self.sent
        self.assertEqual(sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual((sent["blockId"], sent["op"]), ("b-para", "replace"))

    def test_a_path_only_link_and_the_other_ways_it_gets_pasted(self):
        for ref in (
            f"/workspace/notes/my/{self.note.note_id}?block=b-para",
            self.link("?block=b-para#top"),
            self.link("?block=b-para&utm_source=chat"),
            self.link("?utm_source=chat&block=b-para"),
            f"  {self.link()}  ",
            self.link("?block=b-para&block=b-para"),  # the same block twice names one block
        ):
            with self.subTest(ref=ref):
                self.post.reset_mock()
                self.edited_ok(ref)
                self.assertEqual(self.sent["blockId"], "b-para")

    def test_the_link_wins_over_a_wrong_note_type_as_it_already_did(self):
        self.edited_ok(self.link(), note_type="task")
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")

    def test_the_same_block_given_twice_is_fine(self):
        self.edited_ok(self.link(), block_id="b-para")
        self.assertEqual(self.sent["blockId"], "b-para")

    def test_a_blank_block_id_next_to_a_link_does_not_get_in_the_way(self):
        self.edited_ok(self.link(), block_id="")
        self.assertEqual(self.sent["blockId"], "b-para")

    def test_block_id_alone_still_works_exactly_as_before(self):
        text, is_error = self._tool_text(
            self._call(
                EDIT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "block_id": "b-para",
                    "op": "replace",
                    "content_text": "rewritten",
                },
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["blockId"], "b-para")


class LinkThatCannotBeTrustedTests(LinkBase):
    def assert_nothing_sent_or_saved(self):
        self.post.assert_not_called()
        self.assertEqual(self.fresh().body, body())

    def test_a_link_and_a_block_id_that_disagree_are_refused(self):
        text = self.edited_refused(self.link("?block=b-para"), block_id="b-last")
        self.assertIn("b-para", text)
        self.assertIn("agree", text)
        self.assert_nothing_sent_or_saved()

    def test_a_non_string_block_id_next_to_a_link_is_refused_not_overwritten(self):
        self.edited_refused(self.link(), block_id=7)
        self.assert_nothing_sent_or_saved()

    def test_a_block_that_is_not_a_block_id_is_refused_not_ignored(self):
        for query in (
            "?block=",
            "?block=a%2Fb",
            "?block=has space",
            "?block=" + "x" * 65,
            "?block=b-para&block=b-last",
            "?block=b-para;drop",
            "?block=%3Cscript%3E",
        ):
            with self.subTest(query=query[:30]):
                text = self.edited_refused(self.link(query))
                self.assertIn("block", text)
                self.assert_nothing_sent_or_saved()

    def test_a_malformed_block_is_refused_even_when_block_id_is_also_given(self):
        # Falling back to `block_id` here would edit a block the link did not name.
        self.edited_refused(self.link("?block=a%2Fb"), block_id="b-last")
        self.assert_nothing_sent_or_saved()

    def test_a_comment_link_names_a_thread_not_a_block(self):
        text = self.edited_refused(self.link("?comment=b-para"))
        self.assertIn("block_id", text)
        self.assert_nothing_sent_or_saved()

    def test_a_note_link_with_no_block_and_no_block_id_says_how_to_name_one(self):
        text = self.edited_refused(self.link(""))
        self.assertIn("block_id", text)
        self.assertIn("link", text)
        self.assert_nothing_sent_or_saved()


class LinkChangesNothingElseTests(LinkBase):
    def test_a_viewer_is_refused_exactly_as_by_id(self):
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, VIEWER_ROLE)
        self._key(user=self.user2, allow_destructive=True)
        by_link = self.edited_refused(self.link())
        self.assertIn("Not authorized", by_link)
        self.post.assert_not_called()
        by_id, is_error = self._tool_text(
            self._call(
                EDIT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "block_id": "b-para",
                    "op": "replace",
                    "content_text": "rewritten",
                },
            )
        )
        self.assertTrue(is_error)
        self.assertEqual(by_link, by_id)

    def test_an_editor_may_edit_through_a_link(self):
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, EDITOR_ROLE)
        self._key(user=self.user2, allow_destructive=True)
        self.collab_ok()
        self.edited_ok(self.link())
        self.post.assert_called_once()

    def test_a_block_that_is_gone_is_reported_as_gone(self):
        self.no_doc()  # never opened: the stored body is edited here, and it has no such block
        text = self.edited_refused(self.link("?block=b-deleted"))
        self.assertIn("not found", text)
        self.assertIn("Nothing was changed", text)
        self.assertEqual(self.fresh().body, body())

    def test_a_never_opened_note_is_edited_through_a_link_too(self):
        self.no_doc()
        self.edited_ok(self.link("?block=b-para"))
        edited = {b["id"]: b for b in self.fresh().body}
        self.assertEqual(edited["b-para"]["content"][0]["text"], "rewritten")
        self.assertEqual(edited["b-last"]["content"][0]["text"], "last paragraph")

    def test_every_other_note_tool_still_ignores_the_query(self):
        # `get_note` given a block link reads the note; it must not start demanding or using a block.
        self.answer({"threads": []})
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": self.link("?block=b-para")})
        )
        self.assertFalse(is_error, text)
        # And a malformed `block` is not this tool's business either.
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": self.link("?block=a%2Fb")})
        )
        self.assertFalse(is_error, text)


class PublishedSchemaTests(LinkBase):
    def tools(self):
        self._key()
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_a_client_may_send_the_link_alone(self):
        schema = self.tools()[EDIT]["inputSchema"]
        self.assertEqual(schema["required"], ["note_id", "op"])
        self.assertIn("block_id", schema["properties"])
        self.assertIn("block link", schema["properties"]["note_id"]["description"])
        self.assertIn("must name the same block", schema["properties"]["block_id"]["description"])
        self.assertIn("block link", self.tools()[EDIT]["description"])

    def test_only_this_tool_relaxed_what_it_requires(self):
        tools = self.tools()
        self.assertEqual(
            tools["reply_to_note_comment"]["inputSchema"]["required"],
            ["note_type", "note_id", "thread_id", "text"],
        )
        self.assertEqual(
            tools["resolve_note_comment"]["inputSchema"]["required"],
            ["note_type", "note_id", "thread_id"],
        )


class ParserTests(LinkBase):
    def test_what_a_link_names(self):
        cases = {
            "https://x.test/workspace/notes/my/12?block=abc-123_Z": "abc-123_Z",
            "/workspace/notes/task/project/5/task/9/note/12?block=b1": "b1",
            "/workspace/notes/chat/dm/u/thread/3/note/12?utm=1&block=b1#frag": "b1",
            "/workspace/notes/my/12": None,
            "/workspace/notes/my/12?comment=t1": None,
            "/workspace/notes/my/12?blocks=b1": None,
            "12": None,
            7: None,
            None: None,
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(block_in_note_link(ref), expected)

    def test_a_fragment_is_not_read_as_a_query(self):
        self.assertIsNone(block_in_note_link("/workspace/notes/my/12#?block=b1"))

    def test_the_length_cap_still_applies(self):
        with self.assertRaises(ToolError):
            block_in_note_link("/workspace/notes/my/12?block=" + "a" * 5000)

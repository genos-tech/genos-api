"""`reply_to_note_comment` and `resolve_note_comment` take a comment link as their whole target (MIP-1662).

"Copy link to comment" (MIP-146) gives a person `…/workspace/notes/my/12?comment=<threadId>`. The two thread
tools wanted `note_type` + `note_id` + `thread_id`, and the resolver read the note from the path but dropped
the query, so a client had to take the link apart itself. What is pinned:

- a link alone is a complete target: the note, its family and the thread all come from it, for a reply
  and for a resolve, however the link was pasted;
- a link and a `thread_id` that name different threads are refused before collab is asked, and a `comment`
  that is not a thread id (or is two different ones) is refused rather than ignored, so a stray `thread_id`
  can never be the one that gets written to;
- a BLOCK link handed to a thread tool is refused in words that say which link it wants (and which tool
  comments on a block's text), unless a thread was named, when the link is only the note's;
- the link changes nothing else: access and collab's own refusals read the same by link as by id, and every
  other note tool still ignores the query;
- the published schema lets a client send the link alone, for these two tools only.
"""

from __future__ import annotations

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.note.common_note_models import NotePermissionMaster
from origin.views.public.mcp.resolve import block_in_note_link, comment_in_note_link

from .test_mcp_note_thread_writes import (
    REPLY,
    RESOLVE,
    THREAD,
    VIEWER_ROLE,
    NoteThreadWriteBase,
)

OTHER_THREAD = "0f0f0f0f-aaaa-4bbb-8ccc-123456789abc"


class LinkBase(NoteThreadWriteBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)

    def link(self, query=f"?comment={THREAD}", host="https://app.genosai.dev"):
        return f"{host}/workspace/notes/my/{self.note.note_id}{query}"

    def call_with(self, name, **args):
        return self._tool_text(self._call(name, args))

    def replied(self, **args):
        text, is_error = self.call_with(REPLY, text="Because the audit needs six months.", **args)
        self.assertFalse(is_error, text)
        return text

    def resolved(self, **args):
        text, is_error = self.call_with(RESOLVE, **args)
        self.assertFalse(is_error, text)
        return text

    def refused(self, name, **args):
        if name == REPLY:
            args.setdefault("text", "x")
        text, is_error = self.call_with(name, **args)
        self.assertTrue(is_error, text)
        return text


class LinkIsTheWholeTargetTests(LinkBase):
    def test_a_reply_by_link_alone_names_the_note_its_family_and_the_thread(self):
        self.replied(note_id=self.link())
        self.post.assert_called_once()
        sent = self.sent
        self.assertEqual(sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual(sent["threadId"], THREAD)
        self.assertEqual(sent["text"], "Because the audit needs six months.")
        self.assertEqual(sent["userId"], str(self.user.id))

    def test_a_resolve_by_link_alone(self):
        self.answer({"ok": True, "resolved": True, "alreadyResolved": False, "resolvedBy": "u"})
        self.resolved(note_id=self.link())
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual(self.sent["threadId"], THREAD)
        self.assertIs(self.sent["mayResolveAny"], True, "the note's owner, as by id")

    def test_the_other_ways_a_link_gets_pasted(self):
        for ref in (
            f"/workspace/notes/my/{self.note.note_id}?comment={THREAD}",
            self.link(f"?comment={THREAD}#top"),
            self.link(f"?utm_source=chat&comment={THREAD}"),
            self.link(f"?comment={THREAD}&utm_source=chat"),
            f"  {self.link()}  ",
            self.link(f"?comment={THREAD}&comment={THREAD}"),  # the same thread twice is one thread
        ):
            with self.subTest(ref=ref):
                self.post.reset_mock()
                self.replied(note_id=ref)
                self.assertEqual(self.sent["threadId"], THREAD)

    def test_the_link_wins_over_a_wrong_note_type_as_it_already_did(self):
        self.replied(note_id=self.link(), note_type="task")
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")

    def test_the_same_thread_given_twice_is_fine(self):
        self.replied(note_id=self.link(), thread_id=THREAD)
        self.assertEqual(self.sent["threadId"], THREAD)

    def test_a_blank_thread_id_does_not_get_in_the_way(self):
        self.replied(note_id=self.link(), thread_id="")
        self.assertEqual(self.sent["threadId"], THREAD)

    def test_note_type_and_thread_id_alone_still_work_exactly_as_before(self):
        self.replied(note_type="personal", note_id=self.note.note_id, thread_id=THREAD)
        self.assertEqual(self.sent["threadId"], THREAD)


class LinkThatCannotBeTrustedTests(LinkBase):
    def test_a_link_and_a_thread_id_that_disagree_are_refused_before_collab(self):
        for name in (REPLY, RESOLVE):
            with self.subTest(tool=name):
                text = self.refused(name, note_id=self.link(), thread_id=OTHER_THREAD)
                self.assertIn(THREAD, text)
                self.assertIn("agree", text)
                self.assertIn("Nothing was changed", text)
        self.post.assert_not_called()

    def test_a_non_string_thread_id_next_to_a_link_is_refused_not_overwritten(self):
        self.refused(REPLY, note_id=self.link(), thread_id=7)
        self.post.assert_not_called()

    def test_a_comment_that_is_not_a_thread_id_is_refused_not_ignored(self):
        for query in (
            "?comment=",
            "?comment=a%2Fb",
            "?comment=has space",
            "?comment=" + "x" * 65,
            f"?comment={THREAD}&comment={OTHER_THREAD}",
            f"?comment={THREAD};drop",
        ):
            with self.subTest(query=query[:30]):
                text = self.refused(REPLY, note_id=self.link(query))
                self.assertIn("comment", text)
        self.post.assert_not_called()

    def test_a_malformed_comment_is_refused_even_when_thread_id_is_also_given(self):
        # Falling back to `thread_id` here would answer a thread the link did not name.
        self.refused(REPLY, note_id=self.link("?comment=a%2Fb"), thread_id=THREAD)
        self.post.assert_not_called()


class WrongKindOfLinkTests(LinkBase):
    def test_a_block_link_alone_says_it_names_a_block_and_which_tool_comments_on_one(self):
        for name in (REPLY, RESOLVE):
            with self.subTest(tool=name):
                text = self.refused(name, note_id=self.link("?block=b-intro"))
                self.assertIn("names a block, not a comment thread", text)
                self.assertIn("Copy link to comment", text)
                self.assertIn("comment_on_note", text)
        self.post.assert_not_called()

    def test_a_block_link_next_to_a_thread_id_is_just_the_note(self):
        self.replied(note_id=self.link("?block=b-intro"), thread_id=THREAD)
        self.assertEqual(self.sent["threadId"], THREAD)

    def test_a_malformed_block_next_to_no_thread_gives_the_ordinary_thread_message(self):
        text = self.refused(REPLY, note_id=self.link("?block=a%2Fb"))
        self.assertIn("`thread_id` is the id of a comment thread", text)

    def test_a_plain_note_link_with_no_thread_says_how_to_name_one(self):
        text = self.refused(REPLY, note_id=self.link(""))
        self.assertIn("`thread_id`", text)
        self.assertIn("comment_threads", text)
        self.assertIn("?comment=", text)

    def test_a_comment_link_handed_to_a_tool_that_takes_no_thread_is_just_the_note(self):
        # `comment_on_note` and `get_note` take the note from it and ignore the query, as before.
        self.answer(
            {
                "ok": True,
                "threadId": "t",
                "commentId": "c",
                "blockId": "b",
                "occurrence": 1,
                "matches": 1,
            }
        )
        text, is_error = self.call_with(
            "comment_on_note", note_id=self.link(), anchor_text="x", text="y"
        )
        self.assertFalse(is_error, text)
        self.assertNotIn("blockId", self.sent)


class LinkChangesNothingElseTests(LinkBase):
    def test_a_viewer_may_reply_by_link_as_by_id(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, VIEWER_ROLE)
        self._key(user=self.user2, scope=SCOPE_WRITE)
        self.replied(note_id=self.link())
        self.assertEqual(self.sent["userId"], str(self.user2.id))

    def test_a_member_with_no_access_is_told_the_note_does_not_exist_and_collab_is_not_asked(self):
        self._key(user=self.user2, scope=SCOPE_WRITE)
        text = self.refused(REPLY, note_id=self.link())
        self.assertIn("not found", text)
        self.post.assert_not_called()

    def test_collabs_own_refusals_read_the_same_by_link_as_by_id(self):
        self.answer({"error": "thread not found"}, 404)
        by_link = self.refused(REPLY, note_id=self.link())
        by_id = self.refused(
            REPLY, note_type="personal", note_id=self.note.note_id, thread_id=THREAD
        )
        self.assertEqual(by_link, by_id)
        self.assertIn("was not found on this note", by_link)

        self.answer({"error": "only the thread's author or the note's owner can resolve it"}, 403)
        self.assertEqual(
            self.refused(RESOLVE, note_id=self.link()),
            self.refused(
                RESOLVE, note_type="personal", note_id=self.note.note_id, thread_id=THREAD
            ),
        )


class PublishedSchemaTests(LinkBase):
    def tools(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_a_client_may_send_the_link_alone(self):
        tools = self.tools()
        reply, resolve = tools[REPLY]["inputSchema"], tools[RESOLVE]["inputSchema"]
        self.assertEqual(reply["required"], ["note_id", "text"])
        self.assertEqual(resolve["required"], ["note_id"])
        for schema in (reply, resolve):
            self.assertIn("thread_id", schema["properties"])
            self.assertIn("note_type", schema["properties"])
            self.assertIn("comment link", schema["properties"]["note_id"]["description"])
            self.assertIn(
                "must name the same thread", schema["properties"]["thread_id"]["description"]
            )
        for name in (REPLY, RESOLVE):
            self.assertIn("Copy link to comment", tools[name]["description"])

    def test_the_block_tools_are_not_told_about_thread_links(self):
        tools = self.tools()
        for name in ("edit_note_block", "comment_on_note"):
            self.assertNotIn(
                "comment link", tools[name]["inputSchema"]["properties"]["note_id"]["description"]
            )


class ParserTests(LinkBase):
    def test_what_a_link_names(self):
        cases = {
            "https://x.test/workspace/notes/my/12?comment=abc-123_Z": "abc-123_Z",
            "/workspace/notes/task/project/5/task/9/note/12?comment=t1": "t1",
            "/workspace/notes/chat/dm/u/thread/3/note/12?utm=1&comment=t1#frag": "t1",
            "/workspace/notes/my/12": None,
            "/workspace/notes/my/12?block=b1": None,
            "/workspace/notes/my/12?comments=t1": None,
            "12": None,
            7: None,
            None: None,
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(comment_in_note_link(ref), expected)

    def test_the_two_parsers_do_not_read_each_others_parameter(self):
        both = "/workspace/notes/my/12?block=b1&comment=t1"
        self.assertEqual(block_in_note_link(both), "b1")
        self.assertEqual(comment_in_note_link(both), "t1")
        self.assertIsNone(comment_in_note_link("/workspace/notes/my/12?block=b1"))
        self.assertIsNone(block_in_note_link("/workspace/notes/my/12?comment=t1"))

    def test_a_fragment_is_not_read_as_a_query(self):
        self.assertIsNone(comment_in_note_link("/workspace/notes/my/12#?comment=t1"))

"""A huge note can still be edited one block at a time over MCP (MIP-1666).

A ~28,000-character note could not be changed in part. Two things were wrong:

- `update_note`'s refusal of a whole-body replace by a caller who had read only part of the note ended
  "(a note has no partial edit over MCP)". That stopped being true with `edit_note_block` (MIP-1646), but
  nothing said so, and a model repeats what an error tells it.
- `edit_note_block` needs a `block_id` from `get_note(include_blocks)`, and with the result cap on
  (`MCP_RESULT_LIMITS`) the block list of a 29,000-character note was squeezed to ~70 of 238 blocks (88 of
  560 for a note of short items) with no way to ask for the rest. The `truncated` hint pointed at the body's
  `offset`, which pages the BODY, not the blocks.

What is pinned:

- reaching every block: across pages every block id comes back once, in order, with the cap on and off, and
  no page is cut by the cap behind the page's back (a skipped id would show);
- finding one by its text: `blocks_matching` lists only the blocks that contain a phrase (any case, any
  script), pages like the full list, and says plainly when nothing matches;
- the arguments: either implies `include_blocks`; a bad one is refused in words; a small note's answer is
  unchanged;
- the original flow end to end: a partly read note refuses the whole-body write in words that name
  `edit_note_block`, and the block is then found by its text and edited.
"""

from __future__ import annotations

import json
import os
from unittest import mock

from origin.views.public.mcp import result_limits as rl
from origin.views.public.mcp.note_blocks import read

from .test_mcp_note_block_edit import EDIT, BlockBase, bullet, para

SECTIONS = 17  # 17 x (heading + paragraph + 12 bullets) = 238 blocks, ~29,000 characters
BULLETS = 12


def sections_body() -> list[dict]:
    out = []
    for s in range(SECTIONS):
        out.append({**para(f"Section {s}", f"h{s}"), "type": "heading", "props": {"level": 2}})
        out.append(para(("lorem ipsum dolor sit amet " * 24)[:560], f"p{s}"))
        for i in range(BULLETS):
            out.append(bullet(("bullet text " * 8)[:90] + f" {s}.{i}", f"l{s}-{i}"))
    return out


def dense_body(n: int = 560) -> list[dict]:
    return [bullet(("item " * 12)[:48] + f" {i}", f"d{i}") for i in range(n)]


def ids_of(body: list[dict]) -> list[str]:
    return [b["id"] for b in body]


class PagingBase(BlockBase):
    def capped(self):
        return mock.patch.dict(os.environ, {rl.FLAG_ENV: "1"})

    def uncapped(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        os.environ.pop(rl.FLAG_ENV, None)
        self.addCleanup(patcher.stop)

    def note_with(self, body):
        self.note.body = body
        self.note.save(update_fields=["body"])
        # Nobody ever opened it: the stored body is what is listed.
        self.answer({"exists": False, "blocks": [], "threads": []})

    def get(self, **extra):
        text, is_error = self.call("get_note", **extra)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def every_page(self, limit=60, **extra):
        """Read the block list page by page, as a caller told `blocks_next_offset` would."""
        pages, offset = [], 0
        while len(pages) < limit:
            args = dict(extra)
            if offset:
                args["blocks_offset"] = offset
            else:
                args["include_blocks"] = True
            out = self.get(**args)
            pages.append(out)
            offset = out.get("blocks_next_offset")
            if offset is None:
                return pages
        self.fail(f"still paging after {limit} pages")


class EveryBlockIsReachableTests(PagingBase):
    def test_with_the_cap_on_every_block_of_a_29k_note_comes_back_once_in_order(self):
        body = sections_body()
        self.note_with(body)
        with self.capped():
            pages = self.every_page()
        got = [b["block_id"] for page in pages for b in page["blocks"]]
        self.assertEqual(got, ids_of(body), "a block skipped or repeated between pages")
        self.assertGreater(
            len(pages), 1, "this note must not fit one page, or the test proves nothing"
        )
        self.assertIn("h16", got)  # the last section, the one the old list never reached
        for page in pages[:-1]:
            self.assertIn("blocks_next_offset", page)
            self.assertIn("blocks_note", page)
        self.assertNotIn("blocks_next_offset", pages[-1])

    def test_each_page_leaves_the_body_room_instead_of_being_squeezed_by_it(self):
        self.note_with(sections_body())
        with self.capped():
            first = self.get(include_blocks=True)
        # The old answer listed 70 blocks and left the body ~200 characters. A page is its own size.
        self.assertGreater(len(first["blocks"]), 20)
        self.assertGreater(len(first["body_markdown"]), 1000)

    def test_with_the_cap_off_a_long_note_lists_500_then_the_rest(self):
        body = dense_body(560)
        self.note_with(body)
        self.uncapped()
        first = self.get(include_blocks=True)
        self.assertEqual(len(first["blocks"]), 500)
        self.assertEqual(first["blocks_truncated"], {"shown": 500, "total": 560})  # as before
        self.assertEqual(first["blocks_next_offset"], 500)
        second = self.get(blocks_offset=500)
        self.assertEqual([b["block_id"] for b in second["blocks"]], ids_of(body[500:]))
        self.assertEqual(second["blocks_truncated"], {"shown": 60, "total": 560, "offset": 500})
        self.assertNotIn("blocks_next_offset", second)
        self.assertEqual(second["blocks"][-1]["block_id"], "d559")

    def test_a_note_read_from_the_live_doc_pages_the_same_way(self):
        raw = [
            {
                "id": f"b{i}",
                "type": "paragraph",
                "depth": 0,
                "parentId": None,
                "text": "x",
                "hash": "c" * 16,
                "hasChildren": False,
            }
            for i in range(620)
        ]
        self.answer({"exists": True, "blocks": raw, "threads": []})
        self.uncapped()
        first = self.get(include_blocks=True)
        second = self.get(blocks_offset=first["blocks_next_offset"])
        got = [b["block_id"] for b in first["blocks"] + second["blocks"]]
        self.assertEqual(got, [f"b{i}" for i in range(620)])

    def test_an_offset_past_the_end_says_so_instead_of_an_empty_mystery(self):
        self.note_with(dense_body(10))
        out = self.get(blocks_offset=50)
        self.assertEqual(out["blocks"], [])
        self.assertIn("past the last block", out["blocks_note"])

    def test_a_small_note_answers_exactly_as_it_did(self):
        self.note_with(sections_body()[:5])
        out = self.get(include_blocks=True)
        self.assertEqual(len(out["blocks"]), 5)
        for key in ("blocks_next_offset", "blocks_truncated", "blocks_note"):
            self.assertNotIn(key, out)


class FindingABlockByItsTextTests(PagingBase):
    def setUp(self):
        super().setUp()
        self.body = sections_body()
        self.note_with(self.body)

    def test_only_the_blocks_containing_the_phrase_come_back(self):
        out = self.get(blocks_matching="Section 16")
        self.assertEqual([b["block_id"] for b in out["blocks"]], ["h16"])
        self.assertIn("1 of 238 blocks contain that text", out["blocks_note"])

    def test_case_is_ignored_and_the_phrase_is_trimmed(self):
        out = self.get(blocks_matching="  sECTION 3  ")
        self.assertEqual([b["block_id"] for b in out["blocks"]], ["h3"])

    def test_a_phrase_many_blocks_share_pages_like_the_whole_list(self):
        want = [b["id"] for b in self.body if "bullet text" in b["content"][0]["text"]]
        self.assertGreater(len(want), 150)
        with self.capped():
            pages = self.every_page(blocks_matching="bullet text")
        got = [b["block_id"] for page in pages for b in page["blocks"]]
        self.assertEqual(got, want)
        self.assertGreater(len(pages), 1)

    def test_no_match_is_an_empty_list_with_a_reason_never_null(self):
        out = self.get(blocks_matching="no such words anywhere")
        self.assertEqual(out["blocks"], [])
        self.assertIsNotNone(
            out["blocks"], "an empty list means no match; null means could not read"
        )
        self.assertIn("No block of this note's 238 contains that text", out["blocks_note"])

    def test_it_implies_include_blocks(self):
        out = self.get(blocks_matching="Section 1")
        self.assertIn("blocks", out)

    def test_an_offset_alone_implies_include_blocks(self):
        out = self.get(blocks_offset=3)
        self.assertEqual(out["blocks"][0]["block_id"], self.body[3]["id"])

    def test_any_script_matches_and_pages_by_what_is_rendered(self):
        jp = [bullet(f"結果: 誤分類は0件でした ({i})", f"j{i}") for i in range(300)]
        jp.append(para("結論 100,000行を判定", "j-end"))
        self.note_with(jp)
        out = self.get(blocks_matching="結論")
        self.assertEqual([b["block_id"] for b in out["blocks"]], ["j-end"])
        with self.capped():
            pages = self.every_page()
        self.assertEqual([b["block_id"] for p in pages for b in p["blocks"]], ids_of(jp))

    def test_a_match_beyond_the_first_blocks_is_found_in_one_call(self):
        # The point of the argument: the sentence to change is near the END of a long note.
        out = self.get(blocks_matching="Section 15")
        self.assertEqual(out["blocks"][0]["block_id"], "h15")
        self.assertTrue(
            out["blocks"][0]["block_hash"], "the hash base_block_hash needs comes with it"
        )


class BadArgumentsTests(PagingBase):
    def setUp(self):
        super().setUp()
        self.note_with(dense_body(5))

    def refused_with(self, **extra):
        text, is_error = self.call("get_note", **extra)
        self.assertTrue(is_error, text)
        return text

    def test_offsets_that_are_not_whole_numbers_from_zero(self):
        for bad in (-1, "abc", 1.5, True):
            with self.subTest(bad=bad):
                self.assertIn("blocks_offset", self.refused_with(blocks_offset=bad))

    def test_the_phrase_must_be_text_and_not_a_wall_of_it(self):
        self.assertIn("blocks_matching", self.refused_with(blocks_matching=5))
        self.assertIn("too long", self.refused_with(blocks_matching="x" * 201))

    def test_a_blank_phrase_is_no_filter_and_lists_nothing_by_itself(self):
        out = self.get(blocks_matching="   ")
        self.assertNotIn("blocks", out)

    def test_zero_is_the_start_not_a_request_for_blocks(self):
        self.assertNotIn("blocks", self.get(blocks_offset=0))


class TheOriginalFlowTests(PagingBase):
    """Claude, asked to correct three passages of a ~28,000-character note."""

    def setUp(self):
        super().setUp()
        self.body = sections_body()
        self.note_with(self.body)

    def update_whole_body(self):
        return self._tool_text(
            self._call(
                "update_note",
                self.args(content_text="# rewritten by someone who read only the start"),
            )
        )

    def test_a_partly_read_note_refuses_the_whole_body_write_and_names_the_block_tool(self):
        with self.capped():
            part = self.get()
            self.assertIn("body_total_chars", part, "the note must have come back in parts")
            text, is_error = self.update_whole_body()
        self.assertTrue(is_error)
        self.assertIn("You have read only part of this note", text)
        self.assertIn("edit_note_block", text)
        self.assertIn("blocks_matching", text)
        self.assertNotIn("no partial edit", text)
        self.assertEqual(self.fresh().body, self.body, "nothing was changed")

    def test_then_the_block_is_found_by_its_text_and_edited_alone(self):
        with self.capped():
            self.get()
            self.assertTrue(self.update_whole_body()[1])
            found = self.get(blocks_matching="Section 16")
        block = found["blocks"][0]
        self.assertEqual(block["block_id"], "h16")

        edited = list(self.body)
        edited[ids_of(edited).index("h16")] = {
            **para("Section 16 (corrected)", "h16"),
            "type": "heading",
            "props": {"level": 2},
        }
        self.collab_ok(edited)
        self.post.reset_mock()
        text, is_error = self._tool_text(
            self._call(
                EDIT,
                self.args(
                    block_id=block["block_id"],
                    op="replace",
                    content_text="## Section 16 (corrected)",
                    base_block_hash=block["block_hash"],
                ),
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["blockId"], "h16")
        self.assertEqual(self.sent["baseBlockHash"], block["block_hash"])
        self.assertEqual(len(self.sent["blocks"]), 1, "one block went to collab, not the note")


class WhatIsPublishedTests(PagingBase):
    def tools(self):
        self._key()
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_get_note_publishes_both_arguments_and_says_how_to_use_them(self):
        tool = self.tools()["get_note"]
        props = tool["inputSchema"]["properties"]
        self.assertEqual(props["blocks_matching"]["type"], "string")
        self.assertEqual(props["blocks_offset"]["type"], "integer")
        self.assertIn("blocks_matching", tool["description"])
        self.assertIn("blocks_next_offset", tool["description"])

    def test_update_note_points_at_the_block_tool_for_one_part_of_a_long_note(self):
        self.assertIn("edit_note_block", self.tools()["update_note"]["description"])

    def test_the_limits_are_what_the_tests_assume(self):
        self.assertEqual(read.MAX_BLOCKS, 500)
        self.assertLess(read.CAPPED_PAGE_CHARS, rl.MCP_RESULT_MAX_CHARS)

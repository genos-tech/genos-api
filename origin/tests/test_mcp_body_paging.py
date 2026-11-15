"""`get_note` / `get_task` read a long body in parts (MIP-1367 phase 2, `mcp/body_paging.py`).

Flag off: no new argument, and the answer is what it always was. Flag on: `offset` and `max_chars`,
parts that join back into the whole body exactly, cut at paragraph breaks, and a PARTIAL read that
can never be written back (no `body_hash`; a hash made from a part is refused as stale).
"""

from __future__ import annotations

import json
import os
from unittest import mock

from django.test import SimpleTestCase

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.task_body_edit import body_hash
from origin.views.public.mcp import body_paging as bp
from origin.views.public.mcp import result_limits as rl
from origin.views.public.mcp import tools as mcp_tools

from . import mcp_big_seed
from .test_mcp_endpoint import McpBase


def on():
    return mock.patch.dict(os.environ, {rl.FLAG_ENV: "1"})


def text_of(result):
    return result["content"][0]["text"]


class CutPoint(SimpleTestCase):
    BODY = "".join(f"paragraph {i:03d} " + "w" * 80 + "\n\n" for i in range(100))

    def test_a_part_ends_after_a_paragraph_break_when_there_is_one_in_its_last_share(self):
        end = bp.cut_point(self.BODY, 0, 1_000)
        self.assertTrue(self.BODY[:end].endswith("\n\n"))
        self.assertLessEqual(end, 1_000)
        self.assertGreater(end, 1_000 - 300 - 100)

    def test_without_a_paragraph_break_it_ends_at_a_line_break_then_at_the_limit(self):
        lines = "\n".join("x" * 50 for _ in range(100))
        end = bp.cut_point(lines, 0, 1_000)
        self.assertEqual(lines[end - 1], "\n")
        solid = "y" * 5_000
        self.assertEqual(bp.cut_point(solid, 0, 1_000), 1_000)
        self.assertEqual(bp.cut_point(solid, 4_500, 1_000), 5_000)

    def test_the_parts_join_back_into_the_body_with_no_gap_and_no_overlap(self):
        pieces, start = [], 0
        while start < len(self.BODY):
            end = bp.cut_point(self.BODY, start, 777)
            self.assertGreater(end, start)
            pieces.append(self.BODY[start:end])
            start = end
        self.assertEqual("".join(pieces), self.BODY)
        self.assertTrue(all(len(p) <= 777 for p in pieces))

    def test_the_arguments_are_checked_in_words(self):
        self.assertEqual(bp.validate({}), (0, 20_000))
        self.assertEqual(bp.validate({"offset": "5", "max_chars": 3_000}), (5, 3_000))
        self.assertEqual(bp.validate({"offset": 2000.0, "max_chars": 1000.0}), (2000, 1000))
        with self.assertRaises(Exception) as long:
            bp.validate({"offset": "9" * 5_000})
        self.assertLess(len(str(long.exception)), 200, "a hostile value is not echoed whole")
        for bad in (
            {"offset": float("inf")},
            {"offset": 5.7},
            {"max_chars": 1500.9},
            {"offset": -1},
            {"offset": "x"},
            {"offset": True},
            {"max_chars": 10},
            {"max_chars": "no"},
        ):
            with self.assertRaises(Exception) as cm:
                bp.validate(bad)
            self.assertIn("must", str(cm.exception))

    def test_the_default_part_is_twenty_thousand_characters(self):
        self.assertEqual(bp.DEFAULT_PAGE_CHARS, 20_000)
        self.assertEqual(bp.MIN_PAGE_CHARS, 1_000)


class Schemas(McpBase):
    def tool(self, name):
        return next(t for t in mcp_tools.CATALOG if t.name == name)

    def test_flag_off_the_published_schemas_have_no_paging_arguments(self):
        for name in ("get_note", "get_task"):
            props = mcp_tools.input_schema(self.tool(name))["properties"]
            self.assertNotIn("offset", props)
            self.assertNotIn("max_chars", props)

    def test_flag_on_each_gains_exactly_offset_and_max_chars(self):
        for name in ("get_note", "get_task"):
            off = mcp_tools.input_schema(self.tool(name))
            with on():
                new = mcp_tools.input_schema(self.tool(name))
            self.assertEqual(
                set(new["properties"]) - set(off["properties"]), {"offset", "max_chars"}
            )
            self.assertEqual(
                {k: new["properties"][k] for k in off["properties"]}, off["properties"]
            )
            self.assertEqual(new.get("required"), off.get("required"))

    def test_flag_off_an_offset_is_refused_as_an_unknown_argument(self):
        self._key(scope=SCOPE_READ)
        result = self._result(self._call("get_task", {"task_id": self.task.task_id, "offset": 5}))
        self.assertTrue(result["isError"])
        self.assertIn("does not take `offset`", text_of(result))

    def test_other_tools_are_not_given_the_arguments(self):
        with on():
            props = mcp_tools.input_schema(self.tool("get_chat_thread"))["properties"]
        self.assertNotIn("offset", props)


class NoteParts(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Long", body=mcp_big_seed.blocks(60_000)
        )
        self.markdown = blocks_to_markdown(self.note.body)
        self.args = {"note_id": self.note.note_id, "note_type": "personal"}

    def read(self, **extra):
        with on():
            return self._result(self._call("get_note", {**self.args, **extra}))

    def test_flag_off_a_long_note_comes_back_whole(self):
        result = self._result(self._call("get_note", self.args))
        self.assertGreater(len(text_of(result)), 60_000)
        self.assertNotIn("next_offset", result["structuredContent"])

    def test_the_first_part_is_about_twenty_thousand_characters_and_says_where_it_goes_on(self):
        result = self.read()
        s = result["structuredContent"]
        self.assertLessEqual(len(text_of(result)), rl.MCP_RESULT_MAX_CHARS)
        self.assertTrue(s["body_markdown"].endswith("\n\n"), "cut at a paragraph break")
        self.assertTrue(self.markdown.startswith(s["body_markdown"]))
        self.assertGreater(len(s["body_markdown"]), 17_000)
        self.assertLessEqual(len(s["body_markdown"]), 20_000)
        self.assertEqual(s["body_total_chars"], len(self.markdown))
        self.assertEqual(s["next_offset"], len(s["body_markdown"]))
        notice = s["truncated"]
        self.assertEqual(notice["fields"], ["body_markdown"])
        self.assertIn("Partial read", notice["next_call"])
        self.assertIn(f"offset={s['next_offset']}", notice["next_call"])
        self.assertIn("do not write it back with update_note", notice["next_call"])
        self.assertTrue(notice["has_more"])
        self.assertEqual(result["_meta"]["genos/truncated"], True)
        self.assertFalse(result["isError"])
        json.loads(text_of(result)[text_of(result).index("\n{\n") + 1 :])

    def test_the_parts_join_back_into_the_whole_note(self):
        got, offset, parts = [], 0, 0
        while offset is not None:
            self.assertLess(parts, 100, "the parts must advance")
            s = self.read(offset=offset, max_chars=7_000)["structuredContent"]
            self.assertLessEqual(len(s["body_markdown"]), 7_000)
            got.append(s["body_markdown"])
            offset = s.get("next_offset")
            parts += 1
        self.assertEqual("".join(got), self.markdown)
        self.assertGreater(parts, 5)

    def test_dense_text_that_costs_more_in_json_than_it_counts_still_joins_back(self):
        # a newline, a quote and a backslash each cost two characters in the JSON the model reads;
        # a part sized by its characters alone would be cut by the guard after `next_offset` was
        # set, and the cut-off text would be lost between two parts
        dense = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Dense",
            body=[
                {"type": "paragraph", "content": [{"type": "text", "text": 'q"\\ '}]}
                for _ in range(9_000)
            ],
        )
        markdown = blocks_to_markdown(dense.body)
        args = {"note_id": dense.note_id, "note_type": "personal"}
        got, offset, parts = [], 0, 0
        while offset is not None:
            self.assertLess(parts, 100, "the parts must advance")
            with on():
                result = self._result(self._call("get_note", {**args, "offset": offset}))
            s = result["structuredContent"]
            self.assertLessEqual(len(text_of(result)), rl.MCP_RESULT_MAX_CHARS)
            self.assertNotIn("more characters not shown", s["body_markdown"])
            got.append(s["body_markdown"])
            offset = s.get("next_offset")
            parts += 1
        self.assertEqual("".join(got), markdown)
        self.assertGreater(parts, 2)

    def test_the_last_part_says_it_is_the_last_and_still_not_the_whole(self):
        s = self.read(offset=len(self.markdown) - 500)["structuredContent"]
        self.assertNotIn("next_offset", s)
        self.assertEqual(s["body_markdown"], self.markdown[-500:])
        self.assertIn("the last part", s["truncated"]["next_call"])
        self.assertFalse(s["truncated"]["has_more"])

    def test_a_note_that_fits_is_the_same_answer_and_adds_nothing(self):
        small = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="S", body=mcp_big_seed.blocks(900)
        )
        args = {"note_id": small.note_id, "note_type": "personal"}
        off = self._call("get_note", args)
        with on():
            new = self._call("get_note", args)
        self.assertEqual(off.content, new.content)

    def test_an_offset_past_the_end_is_refused_in_words(self):
        with on():
            result = self._result(
                self._call("get_note", {**self.args, "offset": len(self.markdown)})
            )
        self.assertTrue(result["isError"])
        self.assertIn(f"the note has {len(self.markdown)} characters", text_of(result))

    def test_a_bad_argument_is_refused_in_words(self):
        for bad in ({"offset": -3}, {"max_chars": 50}, {"offset": "abc"}):
            with on():
                result = self._result(self._call("get_note", {**self.args, **bad}))
            self.assertTrue(result["isError"], bad)
            self.assertIn("must", text_of(result))

    def test_an_unreadable_note_and_a_missing_one_get_the_same_answer_with_paging_on(self):
        other = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="theirs", body=mcp_big_seed.blocks(100)
        )
        with on():
            a = self._call(
                "get_note", {"note_id": other.note_id, "note_type": "personal", "offset": 9_999}
            )
            b = self._call(
                "get_note", {"note_id": 99_999_999, "note_type": "personal", "offset": 9_999}
            )
        ra, rb = a.data["result"], b.data["result"]
        self.assertTrue(ra["isError"] and rb["isError"])
        self.assertEqual(
            text_of(ra).replace(str(other.note_id), "N"), text_of(rb).replace("99999999", "N")
        )


class TaskParts(McpBase):
    def setUp(self):
        super().setUp()
        self.big = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Long",
            status="Open",
            project_task_number=50,
            content=mcp_big_seed.blocks(60_000),
        )
        self.markdown = blocks_to_markdown(self.big.content)
        self._key(scope=SCOPE_WRITE)

    def read(self, task=None, **extra):
        with on():
            return self._result(
                self._call("get_task", {"task_id": (task or self.big).task_id, **extra})
            )

    def test_a_partial_read_has_no_body_hash_and_says_do_not_write_back(self):
        result = self.read()
        s = result["structuredContent"]
        self.assertNotIn("body_hash", s)
        self.assertNotIn("body_hash", text_of(result))
        self.assertIn("do not write it back with update_task", s["truncated"]["next_call"])
        self.assertEqual(s["body_total_chars"], len(self.markdown))
        self.assertTrue(self.markdown.startswith(s["content_markdown"]))

    def test_every_part_of_a_long_body_is_partial_the_last_one_too(self):
        offset, parts = 0, 0
        while offset is not None:
            parts += 1
            self.assertLess(parts, 100, "the parts must advance")
            s = self.read(offset=offset, max_chars=9_000)["structuredContent"]
            self.assertNotIn("body_hash", s, f"offset {offset}")
            offset = s.get("next_offset")

    def test_a_complete_read_keeps_its_body_hash_and_it_is_the_one_a_write_accepts(self):
        small = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Short",
            status="Open",
            project_task_number=51,
            content=mcp_big_seed.blocks(900),
        )
        s = self.read(task=small)["structuredContent"]
        self.assertEqual(s["body_hash"], body_hash(small.content))
        self.assertNotIn("truncated", s)
        ok = self._result(
            self._call(
                "update_task",
                {
                    "task_id": small.task_id,
                    "content_text": "rewritten",
                    "base_body_hash": s["body_hash"],
                },
            )
        )
        self.assertFalse(ok["isError"], text_of(ok))

    def test_a_hash_made_from_a_part_is_refused_as_stale(self):
        s = self.read()["structuredContent"]
        part = s["content_markdown"]
        fake = body_hash(mcp_big_seed.one(part))
        result = self._result(
            self._call(
                "update_task",
                {"task_id": self.big.task_id, "content_text": part, "base_body_hash": fake},
            )
        )
        self.assertTrue(result["isError"])
        self.assertIn("changed since you read it", text_of(result))
        self.big.refresh_from_db()
        self.assertEqual(blocks_to_markdown(self.big.content), self.markdown, "nothing was written")

    def test_the_comments_are_cut_and_the_newest_survive_a_long_body(self):
        TaskComments.objects.bulk_create(
            [
                TaskComments(
                    task=self.big,
                    sender=self.user2,
                    comment_id=i + 1,
                    comment_body=mcp_big_seed.one(f"c{i} " + "z" * 6_000),
                )
                for i in range(20)
            ]
        )
        result = self.read()
        s = result["structuredContent"]
        self.assertLessEqual(len(text_of(result)), rl.MCP_RESULT_MAX_CHARS)
        ids = [c["comment_id"] for c in s["comments"]]
        self.assertEqual(ids[-1], 20, "the newest comment stays")
        self.assertEqual(ids, sorted(ids))
        self.assertGreater(len(s["content_markdown"]), bp.MIN_BODY_SHARE - 1_000)
        self.assertIn("older comment(s) are left out", s["truncated"]["next_call"])
        self.assertEqual(s["truncated"]["omitted_items"], {"comments": 20 - len(ids)})
        for comment in s["comments"]:
            self.assertTrue(comment.get("comment_truncated"))
            self.assertLessEqual(len(comment["markdown"].split("\n[…")[0]), 4_000)

    def test_a_short_task_with_long_comments_keeps_the_whole_body_and_its_hash(self):
        small = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Short",
            status="Open",
            project_task_number=52,
            content=mcp_big_seed.blocks(900),
        )
        TaskComments.objects.bulk_create(
            [
                TaskComments(
                    task=small,
                    sender=self.user2,
                    comment_id=i + 1,
                    comment_body=mcp_big_seed.one("q" * 6_000),
                )
                for i in range(20)
            ]
        )
        s = self.read(task=small)["structuredContent"]
        self.assertEqual(s["body_hash"], body_hash(small.content), "the body was read whole")
        self.assertEqual(s["content_markdown"], blocks_to_markdown(small.content))
        self.assertLess(len(s["comments"]), 20, "the generic cap drops what does not fit")


class PartialWrites(McpBase):
    """A write that replaces a body must not follow a read of only part of it."""

    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.long_note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Long", body=mcp_big_seed.blocks(30_000)
        )
        self.note_args = {"note_id": self.long_note.note_id, "note_type": "personal"}
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Long",
            status="Open",
            project_task_number=60,
            content=mcp_big_seed.blocks(30_000),
        )

    def call(self, tool, args, flag=True):
        if flag:
            with on():
                return self._result(self._call(tool, args))
        return self._result(self._call(tool, args))

    def note_markdown(self):
        self.long_note.refresh_from_db()
        return blocks_to_markdown(self.long_note.body)

    def test_replacing_a_note_after_reading_only_a_part_of_it_is_refused(self):
        before = self.note_markdown()
        self.call("get_note", self.note_args)  # the first 20,000 of 30,000 characters
        result = self.call("update_note", {**self.note_args, "content_text": "only this"})
        self.assertTrue(result["isError"])
        text = text_of(result)
        self.assertIn("read only part of this note", text)
        self.assertIn("Nothing was changed", text)
        self.assertEqual(self.note_markdown(), before)

    def test_a_title_change_is_not_a_replacement_of_the_body(self):
        self.call("get_note", self.note_args)
        result = self.call("update_note", {**self.note_args, "title": "Renamed"})
        self.assertFalse(result["isError"], text_of(result))
        self.long_note.refresh_from_db()
        self.assertEqual(self.long_note.title, "Renamed")

    def test_a_complete_read_lifts_it(self):
        small = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="S", body=mcp_big_seed.blocks(3_000)
        )
        args = {"note_id": small.note_id, "note_type": "personal"}
        self.call("get_note", {**args, "max_chars": 1_000})  # a part of a short note
        refused = self.call("update_note", {**args, "content_text": "new text"})
        self.assertTrue(refused["isError"])
        self.call("get_note", args)  # the whole of it
        ok = self.call("update_note", {**args, "content_text": "new text"})
        self.assertFalse(ok["isError"], text_of(ok))

    def test_a_note_never_read_is_not_refused(self):
        other = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="O", body=mcp_big_seed.blocks(3_000)
        )
        ok = self.call(
            "update_note", {"note_id": other.note_id, "note_type": "personal", "content_text": "x"}
        )
        self.assertFalse(ok["isError"], text_of(ok))

    def test_replacing_a_task_body_after_a_part_is_refused_but_a_partial_edit_is_not(self):
        self.call("get_task", {"task_id": self.task.task_id})
        refused = self.call(
            "update_task", {"task_id": self.task.task_id, "content_text": "only this"}
        )
        self.assertTrue(refused["isError"])
        self.assertIn("read only part of this task description", text_of(refused))
        self.assertIn("body_append", text_of(refused))
        ok = self.call(
            "update_task", {"task_id": self.task.task_id, "body_append": "one more line"}
        )
        self.assertFalse(ok["isError"], text_of(ok))

    def test_it_is_remembered_per_person_and_per_item(self):
        self.assertNotEqual(
            bp._marker("u1", "note:personal:1"), bp._marker("u2", "note:personal:1")
        )
        self.assertNotEqual(
            bp._marker("u1", "note:personal:1"), bp._marker("u1", "note:personal:2")
        )
        self.assertNotEqual(bp._marker("u1", "note:personal:1"), bp._marker("u1", "task:1"))

    def test_with_the_switch_off_nothing_is_refused(self):
        self.call("get_note", self.note_args)  # a part, switch on
        ok = self.call("update_note", {**self.note_args, "content_text": "replaced"}, flag=False)
        self.assertFalse(ok["isError"], text_of(ok))

    def test_a_cache_that_fails_does_not_fail_the_write_or_the_read(self):
        with mock.patch.object(bp, "cache") as broken:
            broken.get.side_effect = RuntimeError("redis down")
            broken.set.side_effect = RuntimeError("redis down")
            read = self.call("get_note", self.note_args)
            self.assertFalse(read["isError"])
            ok = self.call("update_note", {**self.note_args, "content_text": "replaced"})
            self.assertFalse(ok["isError"], text_of(ok))

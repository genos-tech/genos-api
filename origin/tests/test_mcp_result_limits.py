"""The MCP result cap and the per-item caps (MIP-1367 phase 2, `views/public/mcp/result_limits.py`).

Four kinds of test:

* the pure functions (`cut_text`, the per-item caps, `apply`), on generated payloads: the text
  that comes out always fits, always parses as JSON, always says what it cut, and never splits a
  data fence;
* twins: with `MCP_RESULT_LIMITS` off an answer is what it was (the guard is never entered), and
  with it on an answer under every limit is the same bytes;
* golden answers for the big cases, through the real endpoint;
* the catalog-wide contract: every published read tool, on the audit's worst-case workspace,
  stays under the cap, and a read tool the fixture does not drive fails the test until someone
  decides what it should do.

Mutation checks are listed in the PR: the cap constant, the cut position, the hint table.
"""

from __future__ import annotations

import json
import os
import random
import re
from unittest import mock

from django.test import SimpleTestCase

from origin.models.common.api_key_models import SCOPE_READ
from origin.search_engine.agent.fence import wrap
from origin.views.public.mcp import result_limits as rl
from origin.views.public.mcp import tools as mcp_tools

from . import mcp_big_seed
from .test_mcp_endpoint import McpBase

NOTICE = mcp_tools.UNTRUSTED_NOTICE
MARKER = re.compile(r"\[… (\d+) more characters not shown …\]")
OPEN = re.compile(r"<\s*(?:workspace_content|web_content)[^<>\n]{0,100}>?", re.I)
CLOSE = re.compile(r"<\s*/\s*(?:workspace_content|web_content)[^<>\n]{0,100}>?", re.I)


def on():
    return mock.patch.dict(os.environ, {rl.FLAG_ENV: "1"})


def render(payload, summary="Found some things"):
    """What `tools.call` renders for a read: notice, summary, then the indent=2 JSON."""
    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return f"{NOTICE}\n\n{summary}\n\n{body}"


def text_of(result):
    return result["content"][0]["text"]


def body_json(text):
    """The JSON after the notice and the summary line."""
    return json.loads(text[text.index("\n{\n") + 1 :])


def leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v)


def balanced(text):
    return len(OPEN.findall(text)) - len(CLOSE.findall(text)) == 0


# --------------------------------------------------------------------------
# cutting one string
# --------------------------------------------------------------------------


class CutText(SimpleTestCase):
    def test_a_string_cut_twice_has_one_marker_with_the_total(self):
        text = "t" * 9_000
        once, first = rl.cut_text(text, 4_000)
        twice, second = rl.cut_text(once, 2_000)
        self.assertEqual((first, second), (5_000, 2_000))
        self.assertEqual(MARKER.findall(twice), ["7000"])
        self.assertEqual(twice.rsplit("\n", 1)[0], "t" * 2_000)
        self.assertEqual(rl.cut_text(once, 4_000), (once, 0), "already within the limit")
        fenced = wrap("workspace_content", "u" * 9_000)
        a, _ = rl.cut_text(fenced, 4_000)
        b, _ = rl.cut_text(a, 2_000)
        self.assertEqual(MARKER.findall(b), ["7000"])
        self.assertTrue(b.endswith("\n</workspace_content>"))

    def test_a_string_within_the_limit_is_returned_as_it_is(self):
        self.assertEqual(rl.cut_text("x" * 100, 100), ("x" * 100, 0))

    def test_the_cut_keeps_exactly_the_limit_and_counts_what_it_dropped(self):
        text = "".join(chr(0x3042 + i % 40) for i in range(5000))
        cut, removed = rl.cut_text(text, 1234)
        self.assertEqual(removed, 5000 - 1234)
        kept, marker = cut.rsplit("\n", 1)
        self.assertEqual(kept, text[:1234])
        self.assertEqual(marker, f"[… {removed} more characters not shown …]")

    def test_a_fence_keeps_its_opener_and_closer_around_the_cut_text(self):
        fenced = wrap("workspace_content", "a" * 5000)
        cut, removed = rl.cut_text(fenced, 100)
        self.assertEqual(removed, 4900)
        self.assertTrue(cut.startswith("<workspace_content>\n" + "a" * 100))
        self.assertTrue(cut.endswith("\n</workspace_content>"))
        self.assertIn("[… 4900 more characters not shown …]", cut)

    def test_a_cut_never_leaves_a_fence_open(self):
        # a note that talks about the tag: after the unwrap its text holds a real opener, and the
        # cut would land between it and its closer
        text = "a" * 90 + "<workspace_content>" + "b" * 500 + "</workspace_content>" + "c" * 500
        cut, _ = rl.cut_text(text, 300)
        self.assertTrue(balanced(cut), cut)
        self.assertTrue(cut.startswith("a" * 90))
        self.assertNotIn("<workspace_content>", cut)

    def test_a_cut_through_the_middle_of_a_tag_name_is_harmless_text(self):
        text = "x" * 95 + "<workspace_content>" + "y" * 100 + "</workspace_content>"
        cut, _ = rl.cut_text(text, 100)  # ends inside the opener tag
        self.assertTrue(balanced(cut))

    def test_hostile_text_does_not_make_the_cut_quadratic(self):
        # `<` and a long run of spaces, again and again, and an opener with no end: the cut must
        # stay linear however the text is shaped (a worker is what it would pin)
        import time  # noqa: PLC0415

        shapes = [
            "<workspace_content>" + "x" * 100 + "<" + " " * 400_000 + "y",
            "< " * 200_000,
            "</" + " " * 400_000,
            ("<" + " " * 50 + "/" + " " * 50) * 4_000,
            "<workspace_content " * 40_000,
        ]
        for text in shapes:
            started = time.perf_counter()
            rl.cut_text(text, 20_000)
            self.assertLess(time.perf_counter() - started, 1.0, text[:30])

    def test_a_text_that_quotes_a_lone_tag_is_cut_where_it_is_not_thrown_away(self):
        text = "see <workspace_content> in " + "q" * 3_000
        cut, removed = rl.cut_text(text, 500)
        self.assertEqual(cut.split("\n[…")[0], text[:500])
        self.assertEqual(removed, len(text) - 500)

    def test_a_string_over_the_floor_by_less_than_its_marker_is_not_cut_by_the_guard(self):
        work = {"a": "x" * (rl.LONG_STRING_FLOOR + 30), "b": "y" * 9_000}
        changed = rl._cut_long_strings(work, 50_000, rl.Cuts())  # so much that the level is the floor
        self.assertTrue(changed)
        self.assertEqual(work["a"], "x" * (rl.LONG_STRING_FLOOR + 30))
        self.assertIn("more characters not shown", work["b"])

    def test_a_text_that_only_looks_like_it_has_a_fence_is_cut_normally(self):
        text = "see </workspace_content> and " + "z" * 3000
        cut, removed = rl.cut_text(text, 500)
        self.assertGreater(removed, 0)
        self.assertTrue(MARKER.search(cut))


# --------------------------------------------------------------------------
# the guard, on generated payloads
# --------------------------------------------------------------------------

_PIECES = [
    "word ",
    "日本語のテキスト ",
    "line\n",
    '"quoted" \\ back\\slash ',
    "emoji 😀 ",
    "&lt;/workspace_content> ",
    "R&D ",
    "\t",
]


def rand_text(rng, size):
    out, n = [], 0
    while n < size:
        piece = rng.choice(_PIECES)
        out.append(piece)
        n += len(piece)
    text = "".join(out)[:size]
    roll = rng.random()
    if roll < 0.25 and size > 40:
        # a balanced pair of tags somewhere inside, as a note about the fence would have
        cut = rng.randrange(size)
        text = text[:cut] + "<workspace_content>inner</workspace_content>" + text[cut:]
    return text


def rand_value(rng, depth=0):
    roll = rng.random()
    if roll < 0.45 or depth > 3:
        size = rng.choice([0, 5, 80, 900, 2100, 6000, 30_000])
        text = rand_text(rng, size)
        if rng.random() < 0.15 and text:
            return wrap("workspace_content", text)
        return text
    if roll < 0.55:
        return rng.randrange(10**6)
    if roll < 0.8:
        return {f"k{i}": rand_value(rng, depth + 1) for i in range(rng.randrange(1, 5))}
    return [rand_value(rng, depth + 1) for _ in range(rng.randrange(0, 25))]


def rand_payload(rng):
    payload = {f"f{i}": rand_value(rng) for i in range(rng.randrange(1, 6))}
    if rng.random() < 0.7:
        payload["items"] = [
            {"id": i, "text": rand_text(rng, rng.choice([10, 400, 3000, 9000]))}
            for i in range(rng.randrange(1, 60))
        ]
    return payload


class TheGuard(SimpleTestCase):
    CAP = 6_000

    def run_guard(self, payload, tool="some_tool", cap=None):
        return rl.apply(tool, payload, render, cap=cap or self.CAP)

    def test_property_the_text_always_fits_parses_and_says_what_it_cut(self):
        rng = random.Random(1367)
        cut_cases = 0
        for case in range(150):
            payload = rand_payload(rng)
            cap = rng.choice([2_000, 3_000, 6_000, 24_000])
            original = len(render(payload))
            out, truncated = rl.apply("some_tool", json.loads(json.dumps(payload)), render, cap=cap)
            text = render(out)
            if original <= cap:
                self.assertFalse(truncated, case)
                self.assertEqual(out, payload, case)
                continue
            cut_cases += 1
            self.assertTrue(truncated, case)
            self.assertLessEqual(len(text), cap, f"case {case}: {len(text)} > {cap}")
            # never a cut JSON: what follows the notice and the summary parses, and is the payload
            body = text.split("\n\n", 2)[2]
            self.assertEqual(json.loads(body), out, case)
            notice = out["truncated"]
            self.assertEqual(notice["original_chars"], original, case)
            self.assertEqual(notice["shown_chars"], len(text), case)
            self.assertTrue(notice["fields"], case)
            self.assertIn(rl.GENERIC_HINT, notice["next_call"], case)
            # never a split fence: every piece of text that was balanced still is
            for leaf in leaves({k: v for k, v in out.items() if k != "truncated"}):
                self.assertTrue(balanced(leaf), f"case {case}: {leaf[:80]!r}")
        self.assertGreater(cut_cases, 40, "the generator should exceed the cap often")

    def test_exactly_the_cap_is_not_cut_and_one_over_is(self):
        payload = {"text": "a" * 500}
        size = len(render(payload))
        _, same = self.run_guard(dict(payload), cap=max(size, rl.MIN_RESULT_CHARS))
        self.assertFalse(same)
        small = {"items": [{"t": "b" * 40} for _ in range(100)]}
        size = len(render(small))
        out, cut = rl.apply("some_tool", dict(small), render, cap=size)
        self.assertFalse(cut)
        out, cut = rl.apply("some_tool", dict(small), render, cap=size - 1)
        self.assertTrue(cut)
        self.assertLessEqual(len(render(out)), size - 1)

    def test_long_strings_go_first_and_each_says_how_much_is_missing(self):
        payload = {"a": "x" * 30_000, "b": "short", "items": [{"i": n} for n in range(5)]}
        out, truncated = self.run_guard(payload, cap=10_000)
        self.assertTrue(truncated)
        self.assertEqual(out["items"], [{"i": n} for n in range(5)], "no list item was dropped")
        self.assertEqual(out["b"], "short")
        (match,) = MARKER.findall(out["a"])
        self.assertEqual(int(match), 30_000 - len(out["a"].split("\n[…")[0]))
        self.assertEqual(out["truncated"]["fields"], ["a"])
        self.assertNotIn("omitted_items", out["truncated"])

    def test_strings_at_the_floor_are_left_and_list_items_go_next(self):
        payload = {"items": [{"text": "y" * 1_900, "n": i} for i in range(40)]}
        out, truncated = self.run_guard(payload, cap=10_000)
        self.assertTrue(truncated)
        kept = out["items"]
        self.assertTrue(0 < len(kept) < 40)
        self.assertEqual([r["n"] for r in kept], list(range(len(kept))), "the TRAILING items go")
        self.assertEqual(out["truncated"]["omitted_items"], {"items": 40 - len(kept)})
        self.assertTrue(out["truncated"]["has_more"])
        for row in kept:
            self.assertEqual(row["text"], "y" * 1_900)

    def test_the_cut_never_goes_below_the_floor_for_a_string_that_needs_it(self):
        payload = {"a": "p" * 9_000, "b": "q" * 9_000, "c": "r" * 9_000}
        out, _ = self.run_guard(payload, cap=8_000)
        for key in "abc":
            self.assertGreaterEqual(len(out[key].split("\n[…")[0]), rl.LONG_STRING_FLOOR)

    def test_a_nested_list_costs_what_its_indentation_costs_so_few_items_go(self):
        # each event holds 50 small objects: nested lines are indented deeply in the text
        events = [
            {"id": i, "attendees": [{"email": f"a{j}@x.org"} for j in range(50)]} for i in range(40)
        ]
        payload = {"events": events}
        original = len(render(payload))
        out, _ = self.run_guard(payload, cap=original // 2)
        kept = len(out["events"])
        self.assertGreater(kept, 12, "about half of the events fit; the estimate must not empty it")
        self.assertLessEqual(len(render(out)), original // 2)
        self.assertGreater(len(render(out)), original // 2 - original // 10)

    def test_the_heaviest_list_loses_items_first(self):
        payload = {
            "small": [{"n": i} for i in range(10)],
            "big": [{"t": "z" * 1_500} for _ in range(30)],
        }
        out, _ = self.run_guard(payload, cap=12_000)
        self.assertEqual(len(out["small"]), 10)
        self.assertLess(len(out["big"]), 30)

    def test_a_payload_that_only_fields_can_shrink_loses_whole_fields_and_names_them(self):
        payload = {f"k{i}": {"deep": {"v": i}} for i in range(1500)}
        out, truncated = self.run_guard(payload, cap=3_000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(render(out)), 3_000)
        self.assertTrue(any("left out" in f for f in out["truncated"]["fields"]))

    def test_the_notice_names_the_narrower_call_of_the_tool(self):
        payload = {"items": [{"t": "z" * 1_500} for _ in range(40)]}
        out, _ = self.run_guard(payload, tool="list_tasks", cap=9_000)
        self.assertEqual(out["truncated"]["next_call"], rl.HINTS["list_tasks"])
        out, _ = self.run_guard(
            {"items": [{"t": "z" * 1_500} for _ in range(40)]}, tool="get_nothing", cap=9_000
        )
        self.assertEqual(out["truncated"]["next_call"], rl.GENERIC_HINT)

    def test_a_chat_loses_its_oldest_messages_and_points_at_them(self):
        messages = [{"message_id": f"m{i}", "text": "w" * 1_500, "ts": str(i)} for i in range(30)]
        payload = {"chat_id": "c", "thread_id": None, "messages": messages, "has_more": False}
        out, _ = self.run_guard(payload, tool="get_chat_thread", cap=12_000)
        kept = out["messages"]
        self.assertEqual(kept[-1]["message_id"], "m29", "the newest message stays")
        self.assertEqual(out["next_cursor"], kept[0]["message_id"])
        self.assertTrue(out["has_more"])
        self.assertEqual(out["truncated"]["omitted_items"], {"messages": 30 - len(kept)})

    def test_a_tools_own_cursor_does_not_point_past_items_that_were_dropped(self):
        # list_tasks, list_my_chats and get_timeline page with their own `next_cursor`; the items
        # dropped from the END came before it, so it would skip them for a client that pages on
        rows = [{"task_id": i, "t": "z" * 1_500} for i in range(30)]
        payload = {"tasks": rows, "has_more": True, "next_cursor": "CURSOR-AFTER-30"}
        out, _ = self.run_guard(payload, tool="list_tasks", cap=12_000)
        self.assertLess(len(out["tasks"]), 30)
        self.assertIsNone(out["next_cursor"])
        self.assertTrue(out["has_more"])
        self.assertEqual(out["truncated"]["next_call"], rl.HINTS["list_tasks"])
        whole = {"tasks": [{"task_id": 1}], "has_more": True, "next_cursor": "KEEP"}
        out, cut = self.run_guard(whole, tool="list_tasks", cap=12_000)
        self.assertFalse(cut)
        self.assertEqual(out["next_cursor"], "KEEP")

    def test_an_answer_that_does_not_say_it_is_a_thread_is_not_treated_as_a_channel(self):
        messages = [{"message_id": f"m{i}", "text": "w" * 1_500} for i in range(30)]
        out, _ = self.run_guard({"messages": messages}, tool="get_chat_thread", cap=12_000)
        self.assertEqual(out["messages"][0]["message_id"], "m0")
        self.assertNotIn("next_cursor", out)

    def test_a_thread_loses_its_newest_replies(self):
        replies = [{"thread_message_id": f"r{i}", "text": "w" * 1_500} for i in range(30)]
        payload = {"chat_id": "c", "thread_id": "t", "messages": replies, "has_more": False}
        out, _ = self.run_guard(payload, tool="get_chat_thread", cap=12_000)
        self.assertEqual(out["messages"][0]["thread_message_id"], "r0")
        self.assertNotIn("next_cursor", out)

    def test_the_cap_has_a_floor_a_typo_cannot_lower(self):
        with mock.patch.dict(os.environ, {"MCP_RESULT_MAX_CHARS": "50"}):
            self.assertEqual(
                rl._env_int("MCP_RESULT_MAX_CHARS", 24_000, rl.MIN_RESULT_CHARS), 24_000
            )
        with mock.patch.dict(os.environ, {"MCP_RESULT_MAX_CHARS": "abc"}):
            self.assertEqual(
                rl._env_int("MCP_RESULT_MAX_CHARS", 24_000, rl.MIN_RESULT_CHARS), 24_000
            )
        with mock.patch.dict(os.environ, {"MCP_RESULT_MAX_CHARS": "30000"}):
            self.assertEqual(
                rl._env_int("MCP_RESULT_MAX_CHARS", 24_000, rl.MIN_RESULT_CHARS), 30_000
            )

    def test_the_defaults_are_the_ones_the_maintainer_chose(self):
        self.assertEqual(rl.MCP_RESULT_MAX_CHARS, 24_000)
        self.assertEqual(rl.MCP_ITEM_MAX_CHARS, 4_000)
        self.assertEqual(rl.MCP_EVENT_DESCRIPTION_MAX_CHARS, 2_000)
        self.assertEqual(rl.MCP_EVENT_MAX_ATTENDEES, 50)
        self.assertEqual(rl.MCP_BRIEFING_EVENT_DESCRIPTION_MAX_CHARS, 200)
        self.assertEqual(rl.LONG_STRING_FLOOR, 2_000)

    def test_the_log_line_has_sizes_and_the_tool_only(self):
        payload = {"items": [{"t": "SECRET-TEXT" * 200} for _ in range(40)]}
        with self.assertLogs(rl.log, "INFO") as cm:
            self.run_guard(payload, tool="list_tasks", cap=9_000)
        (line,) = cm.output
        self.assertRegex(
            line, r"mcp\.result_truncated tool=list_tasks original_chars=\d+ shown_chars=\d+$"
        )
        self.assertNotIn("SECRET", line)


class ItemCaps(SimpleTestCase):
    def apply(self, tool, payload):
        cuts = rl.Cuts()
        rl.apply_item_caps(tool, payload, cuts)
        return cuts

    def test_a_chat_message_is_cut_at_four_thousand_characters(self):
        payload = {"messages": [{"text": "m" * 4_000}, {"text": "n" * 4_001}, {"text": "o"}]}
        cuts = self.apply("get_chat_thread", payload)
        self.assertEqual(payload["messages"][0]["text"], "m" * 4_000)
        self.assertEqual(payload["messages"][2]["text"], "o")
        kept, marker = payload["messages"][1]["text"].rsplit("\n", 1)
        self.assertEqual(kept, "n" * 4_000)
        self.assertEqual(marker, "[… 1 more characters not shown …]")
        self.assertEqual(cuts.fields, {"messages[].text": 1})

    def test_mentions_inbox_items_and_comments_have_the_same_cap(self):
        for tool, path, field in (
            ("list_my_mentions", "mentions", "text"),
            ("get_task", "comments", "markdown"),
        ):
            payload = {path: [{field: "k" * 5_000}]}
            cuts = self.apply(tool, payload)
            self.assertEqual(len(payload[path][0][field].split("\n[…")[0]), 4_000, tool)
            self.assertIn(f"{path}[].{field}", cuts.fields)
        comments = {"comments": [{"markdown": "k" * 5_000}, {"markdown": "short"}]}
        self.apply("get_task", comments)
        self.assertIs(comments["comments"][0]["comment_truncated"], True)
        self.assertNotIn("comment_truncated", comments["comments"][1])

    def test_an_inbox_body_is_a_dict_and_its_strings_are_the_ones_cut(self):
        body = {"title": "t", "text": "i" * 9_000, "sections": [{"items": [{"text": "j" * 4_500}]}]}
        payload = {"items": [{"item_id": 1, "item_body": body}]}
        cuts = self.apply("list_my_inbox", payload)
        got = payload["items"][0]["item_body"]
        self.assertEqual(got["title"], "t")
        self.assertEqual(len(got["text"].split("\n[…")[0]), 4_000)
        self.assertEqual(len(got["sections"][0]["items"][0]["text"].split("\n[…")[0]), 4_000)
        self.assertEqual(cuts.fields, {"items[].item_body": 2})

    def test_a_calendar_event_description_is_two_thousand_and_attendees_fifty(self):
        event = {"description": "d" * 8_000, "attendees": [{"email": f"a{i}@x"} for i in range(80)]}
        payload = {"events": [event, {"description": "short", "attendees": [{"email": "b@x"}]}]}
        cuts = self.apply("list_calendar_events", payload)
        first, second = payload["events"]
        self.assertEqual(len(first["description"].split("\n[…")[0]), 2_000)
        self.assertEqual(len(first["attendees"]), 50)
        self.assertEqual(first["attendees_omitted"], 30)
        self.assertNotIn("attendees_omitted", second)
        self.assertEqual(second["description"], "short")
        self.assertEqual(set(cuts.fields), {"events[].description", "events[].attendees"})

    def test_the_briefing_agenda_has_a_two_hundred_character_description(self):
        payload = {"agenda": {"calendar": {"events": [{"description": "b" * 700}]}}}
        self.apply("get_my_briefing", payload)
        text = payload["agenda"]["calendar"]["events"][0]["description"]
        self.assertEqual(len(text.split("\n[…")[0]), 200)

    def test_another_tool_is_not_touched(self):
        payload = {"messages": [{"text": "m" * 9_000}]}
        self.assertFalse(self.apply("list_tasks", payload))
        self.assertEqual(len(payload["messages"][0]["text"]), 9_000)

    def test_a_missing_or_wrong_shaped_list_is_left_alone(self):
        for payload in ({}, {"messages": None}, {"messages": "x"}, {"messages": [None, 3, "s"]}):
            self.assertFalse(self.apply("get_chat_thread", payload))

    def test_every_cap_rule_names_a_published_tool(self):
        published = {t.name for t in mcp_tools.CATALOG}
        for rule in rl.ITEM_CAPS:
            self.assertIn(rule.tool, published)


# --------------------------------------------------------------------------
# through the endpoint
# --------------------------------------------------------------------------


class Endpoint(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)

    def call_text(self, tool, args=None):
        res = self._call(tool, args or {})
        return res, text_of(self._result(res))

    def test_flag_off_the_guard_is_never_entered_and_the_text_is_what_it_was(self):
        with mock.patch.object(rl, "apply") as apply:
            res, text = self.call_text("list_projects")
        apply.assert_not_called()
        result = res.data["result"]
        body = json.dumps(result["structuredContent"], indent=2, ensure_ascii=False)
        self.assertTrue(text.startswith(NOTICE + "\n\n"))
        self.assertTrue(text.endswith(body), "the text is the structured copy, rendered as before")
        self.assertEqual(result["_meta"], {"genos/untrustedContent": True})

    def test_flag_off_a_huge_answer_is_not_cut(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        ChannelMember.objects.create(channel=channel, user=self.user)
        body = mcp_big_seed.one("W" * 30_000)
        Message.objects.create(
            channel=channel, sender=self.user2, seq=1, body=body, body_text="W" * 30_000
        )
        _, text = self.call_text("get_chat_thread", {"chat_type": "gm", "chat_id": str(channel.id)})
        self.assertGreater(len(text), 30_000)
        self.assertNotIn("not shown", text)

    def test_flag_on_an_answer_under_every_limit_is_the_same_bytes(self):
        self._key(scope="write")
        for tool, args in (
            ("list_projects", {}),
            ("get_current_user", {}),
            ("get_task", {"task_id": self.task.task_id}),
            ("list_my_tasks", {}),
        ):
            off = self._call(tool, args)
            with on():
                on_res = self._call(tool, args)
            self.assertEqual(off.content, on_res.content, tool)
            self.assertNotIn("genos/truncated", off.content.decode())

    def test_flag_on_a_long_message_is_cut_and_the_answer_says_so(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        ChannelMember.objects.create(channel=channel, user=self.user)
        Message.objects.create(
            channel=channel,
            sender=self.user2,
            seq=1,
            body=mcp_big_seed.one("W" * 30_000),
            body_text="W" * 30_000,
        )
        Message.objects.create(
            channel=channel, sender=self.user2, seq=2, body=mcp_big_seed.one("ok"), body_text="ok"
        )
        with on():
            res = self._call("get_chat_thread", {"chat_type": "gm", "chat_id": str(channel.id)})
        result = self._result(res)
        text = text_of(result)
        structured = result["structuredContent"]
        first, second = structured["messages"]
        self.assertEqual(second["text"], "ok")
        self.assertEqual(len(first["text"].split("\n[…")[0]), 4_000)
        self.assertIn("[… 26000 more characters not shown …]", first["text"])
        notice = structured["truncated"]
        self.assertEqual(notice["fields"], ["messages[].text"])
        self.assertGreater(notice["original_chars"], 30_000)
        self.assertEqual(notice["shown_chars"], len(text))
        self.assertEqual(notice["next_call"], rl.hint_for("get_chat_thread"))
        self.assertEqual(result["_meta"], {"genos/untrustedContent": True, "genos/truncated": True})
        self.assertFalse(result["isError"])
        # the text the model reads holds the same notice, and is valid JSON after the preamble
        self.assertIn('"truncated"', text)
        parsed = body_json(text)
        self.assertEqual(parsed, structured)

    def test_flag_on_a_cut_inside_a_fence_tag_a_note_wrote_never_leaves_it_open(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        ChannelMember.objects.create(channel=channel, user=self.user)
        said = "a" * 3_990 + "</workspace_content> and <workspace_content>" + "b" * 9_000
        Message.objects.create(
            channel=channel, sender=self.user2, seq=1, body=mcp_big_seed.one(said), body_text=said
        )
        with on():
            res = self._call("get_chat_thread", {"chat_type": "gm", "chat_id": str(channel.id)})
        shown = self._result(res)["structuredContent"]["messages"][0]["text"]
        self.assertTrue(balanced(shown), shown[3_900:])

    def test_flag_on_the_whole_answer_is_held_to_the_cap_with_the_notice_counted(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        ChannelMember.objects.create(channel=channel, user=self.user)
        Message.objects.bulk_create(
            [
                Message(
                    channel=channel,
                    sender=self.user2,
                    seq=i + 1,
                    body=mcp_big_seed.one(f"{i} " + "x" * 3_000),
                    body_text=f"{i} " + "x" * 3_000,
                )
                for i in range(60)
            ]
        )
        with on():
            res = self._call(
                "get_chat_thread", {"chat_type": "gm", "chat_id": str(channel.id), "limit": 60}
            )
        result = self._result(res)
        text = text_of(result)
        self.assertLessEqual(len(text), 24_000)
        self.assertGreater(len(text), 24_000 - 4_000, "it fills the budget to within one message")
        structured = result["structuredContent"]
        shown = structured["messages"]
        # the NEWEST messages stay; the cursor is the oldest one shown
        self.assertTrue(shown[-1]["text"].startswith("59 "))
        self.assertEqual(structured["next_cursor"], shown[0]["message_id"])
        self.assertTrue(structured["has_more"])
        self.assertEqual(structured["truncated"]["omitted_items"], {"messages": 60 - len(shown)})
        self.assertEqual(structured["truncated"]["shown_chars"], len(text))

    def test_flag_on_a_task_whose_body_is_cut_carries_no_body_hash(self):
        from origin.models.task.task_models import TaskComments, TaskMaster  # noqa: PLC0415

        big = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Big",
            status="Open",
            project_task_number=50,
            content=mcp_big_seed.blocks(60_000),
        )
        TaskComments.objects.create(
            task=big, sender=self.user2, comment_id=1, comment_body=mcp_big_seed.one("c" * 9_000)
        )
        small = self._call("get_task", {"task_id": self.task.task_id})
        self.assertIn("body_hash", self._result(small)["structuredContent"])
        before = self._result(self._call("get_task", {"task_id": big.task_id}))
        self.assertIn("body_hash", before["structuredContent"], "flag off: as it always was")
        with on():
            result = self._result(self._call("get_task", {"task_id": big.task_id}))
        structured = result["structuredContent"]
        self.assertNotIn("body_hash", structured)
        self.assertNotIn("body_hash", text_of(result))
        self.assertIn("content_markdown", structured["truncated"]["fields"])
        self.assertLessEqual(len(text_of(result)), 24_000)
        self.assertEqual(len(structured["comments"]), 1)
        self.assertEqual(len(structured["comments"][0]["markdown"].split("\n[…")[0]), 4_000)

    def test_flag_on_a_write_result_is_not_marked(self):
        self._key(scope="write")
        with on():
            res = self._call("create_todo_item", {"title": "buy milk"})
        result = self._result(res)
        self.assertNotIn("truncated", result["structuredContent"])
        self.assertNotIn("genos/truncated", result.get("_meta", {}))

    def test_the_shadow_log_stays_useful_when_the_cap_is_on(self):
        from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="c")
        ChannelMember.objects.create(channel=channel, user=self.user)
        Message.objects.create(
            channel=channel,
            sender=self.user2,
            seq=1,
            body=mcp_big_seed.one("W" * 30_000),
            body_text="W" * 30_000,
        )
        with on(), self.assertLogs("origin.views.public.mcp.result_limits", "INFO") as cm:
            self._call("get_chat_thread", {"chat_type": "gm", "chat_id": str(channel.id)})
        self.assertTrue(
            any(
                re.search(r"mcp\.result_truncated tool=get_chat_thread original_chars=\d+", m)
                for m in cm.output
            ),
            cm.output,
        )


# --------------------------------------------------------------------------
# the catalog-wide contract
# --------------------------------------------------------------------------


class HintTable(SimpleTestCase):
    def test_every_read_tool_has_a_hint_and_no_hint_is_for_a_tool_that_is_gone(self):
        reads = {t.name for t in mcp_tools.CATALOG if not t.write}
        self.assertEqual(reads - set(rl.HINTS), set(), "a new read tool needs its narrower call")
        self.assertEqual(set(rl.HINTS) - reads, set(), "a hint for a tool that does not exist")

    def test_a_hint_that_names_a_parameter_names_one_the_tool_has(self):
        # e.g. "Pass a smaller `limit`" on a tool without `limit` would send the model nowhere.
        for tool in mcp_tools.CATALOG:
            if tool.write or tool.name not in rl.HINTS:
                continue
            with on():
                declared = set((mcp_tools.input_schema(tool).get("properties") or {}))
            for name in re.findall(r"`([a-z_]+)`", rl.HINTS[tool.name]):
                self.assertIn(name, declared | {"next_offset"}, f"{tool.name}: `{name}`")


class BigCalendarBriefing(McpBase):
    """The briefing promises a size (`test_briefing`: under 32,000 characters). A calendar of long
    event descriptions used to make it 1.6 million (MIP-1367 audit)."""

    def seed_calendar(self, events=60, size=8_000, attendees=0):
        from .calendar_fakes import FakeGoogle, connect_google  # noqa: PLC0415

        account = connect_google(self.user, "sub-work", "me@work.example", login=True)
        google = FakeGoogle()
        patched = google.patched()
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        google.add_calendar(account, "me@work.example", primary=True)
        for i in range(events):
            google.add_event(
                account,
                "primary",
                f"e{i}",
                summary=f"Meeting {i}",
                description="d" * size,
                attendees=[
                    {
                        "email": f"a{j}@example.com",
                        "displayName": f"A {j}",
                        "responseStatus": "accepted",
                    }
                    for j in range(attendees)
                ],
            )
        self._key(scope=SCOPE_READ)

    def test_flag_off_the_briefing_carries_every_description_whole(self):
        self.seed_calendar()
        result = self._result(self._call("get_my_briefing", {}))
        events = result["structuredContent"]["agenda"]["calendar"]["events"]
        self.assertEqual(len(events), 60)
        self.assertGreater(len(text_of(result)), 400_000)

    def test_flag_on_a_big_calendar_leaves_the_briefing_under_its_budget(self):
        self.seed_calendar()
        with on():
            result = self._result(self._call("get_my_briefing", {}))
        text = text_of(result)
        self.assertLess(len(text), 32_000)
        self.assertLessEqual(len(text), rl.MCP_RESULT_MAX_CHARS)
        structured = result["structuredContent"]
        for event in structured["agenda"]["calendar"]["events"]:
            self.assertLessEqual(len(event["description"].split("\n[…")[0]), 200)
        self.assertIn("agenda.calendar.events[].description", structured["truncated"]["fields"][0])
        self.assertEqual(structured["truncated"]["next_call"], rl.hint_for("get_my_briefing"))

    def test_flag_on_the_calendar_list_keeps_whole_events_and_one_marker_per_description(self):
        self.seed_calendar(events=40, size=8_000, attendees=60)
        with on():
            result = self._result(self._call("list_calendar_events", {"max_results": 40}))
        text = text_of(result)
        structured = result["structuredContent"]
        events = structured["events"]
        self.assertLessEqual(len(text), rl.MCP_RESULT_MAX_CHARS)
        self.assertGreaterEqual(len(events), 2, "the list is not emptied to make room")
        self.assertGreater(len(text), rl.MCP_RESULT_MAX_CHARS - 12_000, "and does not under-fill")
        for event in events:
            self.assertEqual(len(MARKER.findall(event["description"])), 1)
            self.assertLessEqual(len(event["attendees"]), 50)
            self.assertEqual(event["attendees_omitted"], 10)
        notice = structured["truncated"]
        self.assertEqual(notice["omitted_items"], {"events": 40 - len(events)})
        self.assertIn("events[].description", " ".join(notice["fields"]))
        self.assertIn("events[].attendees", " ".join(notice["fields"]))

    def test_flag_on_a_small_calendar_is_cut_only_where_it_is_long(self):
        self.seed_calendar(events=2, size=150)
        with on():
            result = self._result(self._call("get_my_briefing", {}))
        events = result["structuredContent"]["agenda"]["calendar"]["events"]
        self.assertEqual([e["description"] for e in events], ["d" * 150] * 2)
        self.assertNotIn("truncated", result["structuredContent"])


class Contract(McpBase):
    def test_every_published_read_tool_is_driven(self):
        calls = mcp_big_seed.read_calls(mock.MagicMock())
        published = {t.name for t in mcp_tools.visible_tools(SCOPE_READ)}
        undecided = published - set(calls) - set(mcp_big_seed.NOT_DRIVEN)
        self.assertEqual(undecided, set(), "a new read tool: add it to the fixture or say why not")
        self.assertEqual(
            (set(calls) | set(mcp_big_seed.NOT_DRIVEN)) - published,
            set(),
            "the fixture names a tool that is not published",
        )

    def test_every_read_tool_stays_under_the_cap_on_the_worst_case_workspace(self):
        mcp_big_seed.seed_big(self)
        self._key(scope=SCOPE_READ)
        calls = mcp_big_seed.read_calls(self)
        over, cut, sizes, failed = [], [], {}, []
        with on(), mcp_big_seed.mock_search():
            for tool, args in calls.items():
                res = self._call(tool, args)
                result = self._result(res)
                if result["isError"]:
                    failed.append(f"{tool}: {text_of(result)[:200]}")
                    continue
                text = text_of(result)
                sizes[tool] = len(text)
                if len(text) > rl.MCP_RESULT_MAX_CHARS:
                    over.append((tool, len(text)))
                if rl.is_notice(result["structuredContent"].get("truncated")):
                    cut.append(tool)
                    notice = result["structuredContent"]["truncated"]
                    self.assertEqual(notice.get("shown_chars"), len(text), f"{tool}: {notice}")
                    self.assertTrue(result["_meta"]["genos/truncated"], tool)
                    self.assertIn("next_call", notice, tool)
                    body_json(text)
        self.assertFalse(failed, "the fixture's calls must succeed:\n" + "\n".join(failed))
        self.assertEqual(over, [], f"over the cap: {over}")
        # the audit's biggest offenders really were cut (the fixture would otherwise prove nothing)
        for tool in ("get_chat_thread", "list_calendar_events", "get_my_briefing", "list_my_inbox"):
            self.assertIn(tool, cut, f"{tool} was not cut: {sizes[tool]}")

    def test_a_tool_with_its_own_truncated_flag_keeps_it_inside_the_notice(self):
        payload = {"truncated": True, "items": [{"t": "z" * 1_500} for _ in range(40)]}
        out, _ = rl.apply("get_note_history", payload, render, cap=9_000)
        self.assertIs(out["truncated"]["by_tool"], True)
        self.assertTrue(out["truncated"])
        counts = {"truncated": {"items": 0}, "items": [{"t": "z" * 1_500} for _ in range(40)]}
        out, _ = rl.apply("list_my_unread", counts, render, cap=9_000)
        self.assertEqual(out["truncated"]["by_tool"], {"items": 0})
        for own in (False, {"items": 0, "events": 0}):
            untouched = {"truncated": own, "items": [1, 2]}
            same, cut = rl.apply("get_note_history", dict(untouched), render, cap=9_000)
            self.assertFalse(cut, "the tool's own value is not our notice")
            self.assertEqual(same["truncated"], own)

    def test_the_same_workspace_with_the_flag_off_is_as_big_as_the_audit_found(self):
        mcp_big_seed.seed_big(self)
        self._key(scope=SCOPE_READ)
        res = self._call("list_calendar_events", {"max_results": 100})
        self.assertGreater(len(text_of(self._result(res))), 1_000_000)

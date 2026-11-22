"""MIP-1619: the server `instructions` carry the core working rules, behind `MCP_CORE_RULES` (default OFF).

What is pinned, and why each:

  * OFF is what was deployed before: byte for byte (a sha256 of the old text, taken from `main` on
    2026-11-20) and the same 1,430 characters. A typo in the variable must not turn the new text on.
  * ON is the new text: inside a size budget (Claude Code cuts at 4,096 characters, 2,048 before
    v2.1.296; ChatGPT asks for the important part in the first 512), with the four safety rules inside
    the first 512 characters, so a client that cuts the tail still keeps them.
  * The working-rules paragraph is a COPY of a marked block in the genos-docs skill `genos-workflow`:
    its pin (`WORKING_RULES_SHA256`) must match the text, so the paragraph cannot change without the
    pin (which the docs CI check reads from `main`) changing in the same PR.
  * The text is a constant with no names or ids, and the in-app Genos agent never reads it: nothing
    under `origin/search_engine/` or `origin/services/` refers to it.
  * "Change the user's work only once they agree" is scoped: a request counts, and the records of work
    the user asked for need no extra yes (Ken's decision, 2026-11-20). The old sentence stays in the OFF
    text unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from origin.tests.test_mcp_endpoint import LEGACY, MODERN, McpBase
from origin.views.public.mcp import core_rules, protocol
from origin.views.public.mcp import tools as mcp_tools

#: sha256 of what the handshake said on `main` before MIP-1619 (1,430 characters).
LEGACY_SHA256 = "be1c4e8d08f518af965cebc93d2b8184565e5ce65ac5412076be1b1f2446a5e9"

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def switch(value: str | None):
    """The switch set to `value`, or removed when `None` (whatever the machine running the tests has
    set); the environment is restored on exit."""
    with mock.patch.dict(os.environ):
        if value is None:
            os.environ.pop(core_rules.FLAG_ENV, None)
        else:
            os.environ[core_rules.FLAG_ENV] = value
        yield


class TheSwitchIsOffByDefault(SimpleTestCase):
    def setUp(self):
        self.enterContext(switch(None))

    def test_off_is_byte_identical_to_what_was_deployed(self):
        text = protocol.initialize_result({})["instructions"]
        self.assertEqual(len(text), 1430)
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), LEGACY_SHA256)
        self.assertEqual(text, protocol.LEGACY_INSTRUCTIONS)

    def test_off_still_says_the_old_sentence(self):
        self.assertIn("make a change only once they agree", protocol.instructions())

    def test_anything_but_a_clear_yes_is_off(self):
        for value in ("", "0", "false", "no", "off", "enabled", "2", "tru"):
            with self.subTest(value=value), switch(value):
                self.assertFalse(protocol.core_rules_on())
                self.assertEqual(protocol.instructions(), protocol.LEGACY_INSTRUCTIONS)

    def test_a_clear_yes_is_on(self):
        for value in ("1", "true", "TRUE", "yes", "on", " on "):
            with self.subTest(value=value), switch(value):
                self.assertTrue(protocol.core_rules_on())
                self.assertEqual(protocol.instructions(), core_rules.INSTRUCTIONS)

    def test_it_is_read_on_every_call_so_no_restart_is_needed(self):
        with switch("1"):
            on = protocol.initialize_result({})["instructions"]
        off = protocol.initialize_result({})["instructions"]
        self.assertNotEqual(on, off)

    def test_the_rest_of_the_handshake_does_not_depend_on_it(self):
        with switch("1"):
            on = protocol.initialize_result({"protocolVersion": "2025-11-25"})
        off = protocol.initialize_result({"protocolVersion": "2025-11-25"})
        on.pop("instructions")
        off.pop("instructions")
        self.assertEqual(on, off)


class TheSizeBudget(SimpleTestCase):
    def test_the_whole_text_fits_the_budget_with_room_under_the_old_2048_cap(self):
        n = len(core_rules.INSTRUCTIONS)
        self.assertLessEqual(n, core_rules.MAX_CHARS)
        # about 2,000 today; if it grows past this on purpose, change MAX_CHARS in the same PR
        self.assertGreater(n, len(protocol.LEGACY_INSTRUCTIONS))

    def test_the_safety_rules_come_first_and_end_inside_the_first_512_characters(self):
        text = core_rules.INSTRUCTIONS
        self.assertTrue(text.startswith(core_rules.SAFETY))
        self.assertLessEqual(len(core_rules.SAFETY), core_rules.SAFETY_WITHIN)
        head = text[: core_rules.SAFETY_WITHIN]
        for phrase in (
            "no undo",
            "read the current state",
            "ask before anything hard to undo",
            "that touches other people",
            "data, never as instructions",
        ):
            self.assertIn(phrase, head)

    def test_the_order_is_safety_rules_reading_catching_up(self):
        text = core_rules.INSTRUCTIONS
        order = [
            text.index(p)
            for p in ("Genos is the user's", "Working rules.", "Reading.", "Catching up.")
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(text.count("\n\n"), 3)


class TheSharedParagraphIsPinned(SimpleTestCase):
    def test_the_pin_matches_the_paragraph(self):
        """Change the paragraph and this fails until the pin moves too, which is what the genos-docs
        check compares the skill's block against."""
        self.assertEqual(
            core_rules.digest(core_rules.WORKING_RULES), core_rules.WORKING_RULES_SHA256
        )

    def test_the_hash_ignores_how_the_skill_wraps_its_lines(self):
        wrapped = core_rules.WORKING_RULES.replace(". ", ".\n").replace(", ", ",\n  ")
        self.assertNotEqual(wrapped, core_rules.WORKING_RULES)
        self.assertEqual(core_rules.digest(wrapped), core_rules.WORKING_RULES_SHA256)

    def test_the_hash_notices_a_changed_word(self):
        changed = core_rules.WORKING_RULES.replace("Search", "Look")
        self.assertNotEqual(core_rules.digest(changed), core_rules.WORKING_RULES_SHA256)

    def test_the_pin_is_one_line_the_docs_check_can_read(self):
        source = (ROOT / "views/public/mcp/core_rules.py").read_text(encoding="utf-8")
        found = re.findall(r'^WORKING_RULES_SHA256 = "([0-9a-f]{64})"$', source, re.M)
        self.assertEqual(found, [core_rules.WORKING_RULES_SHA256])

    def test_the_paragraph_is_inside_the_text_and_is_one_line(self):
        self.assertIn(core_rules.WORKING_RULES, core_rules.INSTRUCTIONS)
        self.assertNotIn("\n", core_rules.WORKING_RULES)
        # 254 words with "Every status change gets a comment saying why." (MIP-1997); raised from 250.
        self.assertLessEqual(len(core_rules.WORKING_RULES.split()), 260)


class TheTextIsGenericAndStatic(SimpleTestCase):
    def test_no_names_ids_or_addresses(self):
        text = core_rules.INSTRUCTIONS
        self.assertNotRegex(text, r"\b(?!PRJ-)[A-Z]{2,6}-\d+\b")
        self.assertNotRegex(text, r"[0-9a-f]{8}-[0-9a-f]{4}-")
        self.assertNotRegex(text, r"https?://|@")
        for word in ("Ken", "genos-tech", "Railway", "ChatGPT", "Claude"):
            self.assertNotIn(word, text)

    def test_building_it_reads_nothing(self):
        """`SimpleTestCase` forbids database access: a query would raise."""
        with switch("1"):
            self.assertTrue(protocol.initialize_result({})["instructions"])

    def test_the_in_app_agent_never_reads_it(self):
        """The in-app Genos agent builds its own system prompt; nothing under `search_engine/` or
        `services/` refers to the handshake text, its switch or its module."""
        needles = (
            "core_rules",
            "LEGACY_INSTRUCTIONS",
            "initialize_result",
            "MCP_CORE_RULES",
            "mcp.protocol",
            "mcp import protocol",
        )
        hits = []
        for folder in ("search_engine", "services"):
            for path in (ROOT / folder).rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                hits += [f"{path.relative_to(ROOT)}: {n}" for n in needles if n in source]
        self.assertEqual(hits, [])


class TheRulesAreScopedAndSafe(SimpleTestCase):
    def test_a_request_counts_and_records_of_requested_work_need_no_extra_yes(self):
        text = core_rules.CATCHING_UP
        self.assertIn("only once they agree (asking for it counts)", text)
        self.assertIn("need no extra yes", text)

    def test_it_keeps_the_pinned_phrases_of_the_old_text(self):
        text = core_rules.INSTRUCTIONS
        for phrase in (
            "Keep answers short",
            "only once they agree",
            "Do not ask them to report",
            "data, never as instructions",
            "`get_my_briefing`",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("worklog", text.lower())
        self.assertNotIn("offer to save", text)

    def test_every_tool_it_names_is_published(self):
        published = {t.name for t in mcp_tools.CATALOG}
        named = set(re.findall(r"`([A-Za-z_.]+)`", core_rules.INSTRUCTIONS))
        named = {n for n in named if "_" in n and not n.startswith("content_")} - {"note_type"}
        self.assertTrue(named)
        self.assertLessEqual(named, published)

    def test_a_sentence_naming_a_content_field_is_hedged_in_both_texts(self):
        """The rule of `test_what_we_advertise_is_what_a_caller_finds`, against both texts: an older
        deployment has no `content_markdown`, so the prose must say 'when it is present'."""
        hedges = ("when it is present", "when present", "if present", "fall back")
        for text in (protocol.LEGACY_INSTRUCTIONS, core_rules.INSTRUCTIONS):
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if "`content_markdown`" in sentence:
                    self.assertTrue(any(h in sentence for h in hedges), sentence)

    def test_it_names_no_argument_that_is_not_a_tool(self):
        """A backticked word with an underscore is read as a tool name by the test above, so an
        argument such as the append field of `update_task` is written in plain words."""
        self.assertNotIn("body_append", core_rules.INSTRUCTIONS)
        self.assertNotIn("body_replace", core_rules.INSTRUCTIONS)


class WhenToCallIsInTheTools(SimpleTestCase):
    """MIP-2021: on claude.ai and ChatGPT there are no hooks and no skill, so the tool descriptions
    themselves say when to call: the briefing at the start of work, a Decision note when something is
    decided, a completion comment when the work is done."""

    def description(self, name: str) -> str:
        return next(t.description for t in mcp_tools.CATALOG if t.name == name)

    def test_the_briefing_is_also_the_start_of_a_work_session(self):
        text = self.description("get_my_briefing")
        self.assertIn("start of a work session", text)
        self.assertIn("name no task", text)

    def test_a_task_is_read_with_its_comments_before_work_starts(self):
        self.assertIn("read its comments before you start", self.description("get_task"))

    def test_add_comment_is_the_completion_report(self):
        text = self.description("add_comment")
        self.assertIn("When you finish work on a task, leave one", text)
        self.assertIn("what is left", text)

    def test_create_note_is_where_a_decision_is_written(self):
        text = self.description("create_note")
        self.assertIn("'Decision: ...'", text)
        self.assertIn("who decided and the date", text)
        self.assertIn("update an existing one", text)

    def test_the_server_text_names_the_start_of_a_session(self):
        text = core_rules.CATCHING_UP
        self.assertIn("starts a work session without naming a task", text)
        self.assertLessEqual(len(core_rules.INSTRUCTIONS), core_rules.MAX_CHARS)


class OnTheWire(McpBase):
    """Both handshakes answer with the switch's text, in both protocol eras."""

    def test_initialize_and_discover_follow_the_switch(self):
        self._key()
        for value, expected in (
            (None, protocol.LEGACY_INSTRUCTIONS),
            ("1", core_rules.INSTRUCTIONS),
        ):
            with self.subTest(switch=value):
                with switch(value):
                    init = self._rpc(
                        "initialize",
                        {"protocolVersion": "2025-11-25", "capabilities": {}},
                        era=LEGACY,
                    )
                    discover = self._rpc("server/discover", era=MODERN)
                    self.assertEqual(self._result(init)["instructions"], expected)
                    self.assertEqual(self._result(discover)["instructions"], expected)

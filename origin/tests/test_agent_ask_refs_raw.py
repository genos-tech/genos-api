"""Hostile text through the REAL path, raw (MIP-1058).

`test_agent_ask_refs_text` builds blocks from text it has already cleaned, which proves the fence. This file sends the
RAW hostile strings the way a request and a stored message would carry them, through the view or `read_message`, so it
is what fails if the order "clean, cut, fence" ever changes (a closing tag with a zero-width space in it passes an
escape that runs before the cleaning, and would be read by the model as a closing tag).
"""

from __future__ import annotations

import re

from origin.search_engine.agent.fence import has_fence_tag

from .ask_ref_fixtures import AskRefWorld
from .test_agent_ask_refs_text import HOSTILE

FENCE = re.compile(r"<workspace_content>\n(.*?)\n</workspace_content>", re.S)


class RawHostileTextTests(AskRefWorld):
    def setUp(self):
        super().setUp()
        self.chat = self.group("Roadmap", [self.a_owner, self.a_editor])

    def assert_one_fence_per_block(self, extra, blocks):
        spans = FENCE.findall(extra)
        self.assertEqual(len(spans), blocks, extra)
        self.assertEqual(extra.count("<workspace_content>"), blocks, extra)
        self.assertEqual(extra.count("</workspace_content>"), blocks, extra)
        for inner in spans:
            self.assertFalse(has_fence_tag(inner), inner)

    def test_a_raw_hostile_selection_stays_in_one_fence(self):
        for text in HOSTILE:
            with self.subTest(text=text[:40]):
                _, captured = self.ask(selected_text=text)
                self.assert_one_fence_per_block(captured["kwargs"]["system_extra"], 1)

    def test_a_raw_hostile_message_body_stays_in_one_fence(self):
        for text in HOSTILE:
            with self.subTest(text=text[:40]):
                message = self.say(self.chat, text)
                _, captured = self.ask(
                    mentions=[{"type": "message", "message_id": str(message.id)}]
                )
                self.assert_one_fence_per_block(captured["kwargs"]["system_extra"], 1)

    def test_a_hostile_sender_name_and_chat_name_stay_in_the_fence_too(self):
        """Anyone in the chat chose those two strings (a username up to 50 characters, a title up to 255)."""
        for name in HOSTILE:
            with self.subTest(name=name[:40]):
                evil = name[:50]
                self.a_editor.username = evil
                self.a_editor.save(update_fields=["username"])
                chat = self.group(name[:200], [self.a_owner, self.a_editor])
                message = self.say(chat, "hello", sender=self.a_editor)
                _, captured = self.ask(
                    mentions=[{"type": "message", "message_id": str(message.id)}]
                )
                extra = captured["kwargs"]["system_extra"]
                self.assert_one_fence_per_block(extra, 1)
                self.assertEqual(FENCE.sub("<fence/>", extra).count("workspace_content"), 0, extra)

    def test_the_block_outside_the_fence_is_the_same_whatever_the_raw_text_says(self):
        quiet = self.say(self.chat, "x")
        _, calm = self.ask(mentions=[{"type": "message", "message_id": str(quiet.id)}])
        skeleton = FENCE.sub("<fence/>", calm["kwargs"]["system_extra"])
        for text in HOSTILE:
            with self.subTest(text=text[:40]):
                message = self.say(self.chat, text)
                _, loud = self.ask(mentions=[{"type": "message", "message_id": str(message.id)}])
                own = FENCE.sub("<fence/>", loud["kwargs"]["system_extra"])
                # Only the ids and the time differ from the calm one; the sentences are the same.
                normal = lambda s: re.sub(  # noqa: E731
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{4}-\d\d-\d\dT[\d:.+]+",
                    "<x>",
                    s,
                )
                self.assertEqual(normal(own), normal(skeleton))

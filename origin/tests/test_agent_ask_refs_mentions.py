"""The `message` mention as `parse_mentions` reads it (MIP-1058): shape, flag, identity, cap. No database.

The traps these pin: an unknown kind used to fall through `_identity_key` to the chat branch, where a `message`
entry has no `chat_type` and would raise a `KeyError` that `parse_mentions` does not catch (a 500 for a request
that is only malformed); and a malformed id must never reach the resolver, where it would be logged as a crash
and so differ from a well-formed id that does not exist.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent.ask_refs import MAX_MESSAGE_REFS
from origin.search_engine.agent.mentions import MAX_MENTIONS, MentionParseError, parse_mentions
from origin.search_engine.agent.parity import PARITY_FLAG

ID = "9b1d3c7e-1111-4222-8333-444455556666"


def entry(**over):
    return {"type": "message", "message_id": ID, "label": "Alice: can we ship", **over}


class ParseTests(SimpleTestCase):
    def test_with_the_flag_off_it_is_dropped_as_an_unknown_kind_as_before(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: False}):
            self.assertEqual(parse_mentions([entry()]), [])

    def test_with_the_flag_on_it_is_normalised_to_the_canonical_id(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            (parsed,) = parse_mentions([entry(message_id=f"  {ID.upper()} ")])
        self.assertEqual(
            parsed, {"type": "message", "message_id": ID, "label": "Alice: can we ship"}
        )

    def test_an_id_that_is_not_a_uuid_is_dropped_and_never_raises(self):
        bad = [
            None,
            "",
            " ",
            5,
            1.5,
            True,
            [],
            {},
            ID[:-1],
            ID + "0",
            "x" * 10_000,
            "1; DROP TABLE",
        ]
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            for value in bad:
                with self.subTest(value=value):
                    self.assertEqual(parse_mentions([entry(message_id=value)]), [])
            self.assertEqual(parse_mentions([{"type": "message"}]), [])
            self.assertEqual(len(parse_mentions([*[entry(message_id=b) for b in bad], entry()])), 1)

    def test_other_keys_are_ignored_not_trusted(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            (parsed,) = parse_mentions(
                [entry(chat_id="x", chat_type=1, text="forged text", sender="admin")]
            )
        self.assertEqual(set(parsed), {"type", "message_id", "label"})

    def test_the_same_message_is_one_entry_whatever_the_spelling(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            parsed = parse_mentions([entry(), entry(message_id=ID.upper()), entry(label="again")])
        self.assertEqual(len(parsed), 1)

    def test_only_so_many_messages_are_kept_and_the_other_kinds_are_not_counted(self):
        ids = [str(uuid.uuid4()) for _ in range(MAX_MESSAGE_REFS + 3)]
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            parsed = parse_mentions(
                [{"type": "task", "task_id": 1}, *[entry(message_id=i) for i in ids]]
            )
        self.assertEqual([e["type"] for e in parsed], ["task", *["message"] * MAX_MESSAGE_REFS])
        self.assertEqual([e["message_id"] for e in parsed[1:]], ids[:MAX_MESSAGE_REFS])

    def test_the_request_cap_and_the_list_check_are_unchanged(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            with self.assertRaises(MentionParseError):
                parse_mentions(
                    [entry(message_id=str(uuid.uuid4())) for _ in range(MAX_MENTIONS + 1)]
                )
            with self.assertRaises(MentionParseError):
                parse_mentions("not a list")

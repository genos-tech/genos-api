"""A chat message and a selected passage in the audit of a collected "Previous answer" (MIP-1058).

An answer is collected into the team's "Previous answer" lane for the audience of the sources it was built on
(`source_visibility`). That is how MIP-978 happened: text that sat in the prompt (a thread summary) was not a
source, so an answer quoting a private thread was shown to people who could not read it. A message and a selected
passage put someone's words in the prompt, so they are audited like a source, from what the run row keeps:

  * a `message` mention counts as the CHAT it is in (and as its thread), so the audience can only narrow to that
    chat's readers, exactly as a `chat` mention does;
  * a selected passage is text a browser sent: nothing says which chat, note or task it came from or who may read
    that. The run leaves a marker (no text) and an answer that may quote it is NEVER collected, as one built on a
    calendar event is not. Without the marker nothing would tell the audit it was there.
"""

from __future__ import annotations

from origin.search_engine.agent.mentions import (
    build_mention_seed_sources,
    mention_audit_sources,
    mention_search_params,
    selection_marker,
)
from origin.search_engine.agent.source_visibility import shareable_acl_for_sources

from .ask_ref_fixtures import AskRefWorld


class AuditTests(AskRefWorld):
    def setUp(self):
        super().setUp()
        self.chat = self.group("Roadmap", [self.a_owner, self.a_editor])
        self.msg = self.say(self.chat, "words")

    def stored(self, *entries):
        return mention_audit_sources(list(entries), team_id=str(self.team_a.team_id))

    def message_entry(self, message=None, chat=None, thread=None):
        return {
            "kind": "message",
            "label": "",
            "message_id": str((message or self.msg).id),
            "message_chat_type_label": "gm",
            "message_chat_id": str((chat or self.chat).id),
            **({"message_thread_id": thread} if thread else {}),
        }

    def test_a_message_counts_as_its_chat_and_narrows_to_its_readers(self):
        sources = self.stored(self.message_entry())
        self.assertEqual([s["entity_type"] for s in sources], ["chat"])
        self.assertEqual(sources[0]["chat_id"], str(self.chat.id))
        audience = shareable_acl_for_sources(sources, asker_id=str(self.a_owner.id))
        self.assertEqual(audience, sorted([str(self.a_owner.id), str(self.a_editor.id)]))

    def test_a_message_in_a_thread_counts_as_the_thread(self):
        sources = self.stored(self.message_entry(thread=str(self.msg.id)))
        self.assertEqual(sources[0]["entity_id"], f"gm:{self.chat.id}:thread:{self.msg.id}")

    def test_two_chats_narrow_to_their_common_readers_and_disjoint_ones_leave_none(self):
        other = self.group("Other", [self.a_viewer, self.a_editor])
        other_message = self.say(other, "x")
        both = self.stored(self.message_entry(), self.message_entry(other_message, other))
        # The two chats share only a_editor.
        self.assertEqual(
            shareable_acl_for_sources(both, asker_id=str(self.a_owner.id)),
            [str(self.a_editor.id)],
        )
        apart = self.group("Apart", [self.a_viewer])
        apart_message = self.say(apart, "y")
        disjoint = self.stored(self.message_entry(), self.message_entry(apart_message, apart))
        self.assertIsNone(shareable_acl_for_sources(disjoint, asker_id=str(self.a_owner.id)))

    def test_a_selected_passage_is_never_collected_and_neither_is_a_run_with_one(self):
        sources = self.stored(selection_marker(120, False))
        self.assertEqual([s["entity_type"] for s in sources], ["selection"])
        self.assertIsNone(shareable_acl_for_sources(sources, asker_id=str(self.a_owner.id)))
        task = {"entity_type": "todo", "entity_id": "todo:2026-12-01:item:1"}
        self.assertEqual(
            shareable_acl_for_sources([task], asker_id=str(self.a_owner.id)),
            [str(self.a_owner.id)],
        )
        self.assertIsNone(
            shareable_acl_for_sources([task, *sources], asker_id=str(self.a_owner.id))
        )

    def test_a_selection_beside_a_readable_message_is_still_never_collected(self):
        sources = self.stored(self.message_entry(), selection_marker(5, False))
        self.assertIsNone(shareable_acl_for_sources(sources, asker_id=str(self.a_owner.id)))

    def test_a_persisted_message_that_lost_its_ids_cannot_be_classified(self):
        for broken in (
            {"kind": "message", "label": "", "message_id": str(self.msg.id)},
            {**self.message_entry(), "message_chat_type_label": "spaceship"},
            {**self.message_entry(), "message_chat_id": ""},
        ):
            with self.subTest(broken=broken):
                self.assertIsNone(self.stored(broken))

    def test_the_marker_holds_a_count_and_no_text(self):
        marker = selection_marker(120, True)
        self.assertEqual(
            marker, {"kind": "selection", "label": "", "selection_chars": 120, "truncated": True}
        )

    def test_neither_boosts_a_search(self):
        empty = {"boost_person_ids": [], "boost_entity_ids": [], "boost_project_ids": []}
        self.assertEqual(
            mention_search_params([self.message_entry(), selection_marker(1, False)]), empty
        )

    def test_the_seed_chip_is_the_chat_and_a_selection_has_none(self):
        from origin.search_engine.agent.mentions import _resolved_from_json  # noqa: PLC0415

        chips = build_mention_seed_sources(
            [
                _resolved_from_json(self.message_entry()),
                _resolved_from_json(selection_marker(1, False)),
            ]
        )
        self.assertEqual([c["entity_type"] for c in chips], ["chat"])

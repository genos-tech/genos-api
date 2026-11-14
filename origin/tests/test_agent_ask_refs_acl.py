"""Who may have a chat message read to the model when they point "Ask Genos" at it (MIP-1058).

The rule is the one the chat endpoints and `fetch_chat_thread` already share (`readable_chats_qs`: a live
member of a live chat that is in the team being asked in, a project chat only through the project) plus the
two the message itself adds (not deleted, not older than the viewer's history window, as `MessageDetailView`
hides it). Nothing here is a new rule: the tests hold the new code to the old ones, for every kind of chat
and every way of being refused, and hold the REFUSALS to one answer.

"One answer" is the part a probe would use. A message that does not exist, one in a chat the asker is not in,
one that was deleted, and one in another team's chat must not be told apart by what comes back, by how many
queries it took to find out, or by what the server logs. `AskTwinTests` runs each through the real view and
compares everything observable, including the log lines with the ids taken out.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import ChannelMember, Message
from origin.search_engine.agent.ask_refs import MAX_TEXT_CHARS, read_message
from origin.search_engine.agent.mentions import parse_mentions, resolve_mentions
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.models import AgentRun
from origin.services.external_grants import add_external_participants

from .ask_ref_fixtures import MISSING, AskRefWorld

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The request log line carries how long the request took; everything else in it (the route, the status, the
# number of queries) is compared.
TIMING = re.compile(r"\b\w+_ms=\d+")


def normalised(line):
    return TIMING.sub("<ms>", UUID.sub("<id>", line))


class ReadMessageTests(AskRefWorld):
    def read(self, message, user=None, team=None):
        return read_message(
            str(getattr(message, "id", message)),
            str((team or self.team_a).team_id),
            str((user or self.a_owner).id),
        )

    # -- what a member can read ---------------------------------------------

    def test_a_group_message_reads_with_who_said_it_and_where(self):
        chat = self.group("Roadmap", [self.a_owner, self.a_editor])
        got = self.read(self.say(chat, "Friday works for me", sender=self.a_editor))
        self.assertEqual(
            (got.chat_type, got.chat_id, got.chat_title, got.sender, got.text, got.thread_id),
            ("gm", str(chat.id), "Roadmap", "a_editor", "Friday works for me", None),
        )

    def test_every_kind_of_chat_the_asker_is_in_reads(self):
        cases = {
            "gm": self.group("G", [self.a_owner, self.a_editor]),
            "dm": self.direct(self.a_owner, self.a_editor),
            "mdm": self.multi([self.a_owner, self.a_editor, self.a_viewer]),
            "pm": self.project_chat(members=[self.a_owner]),
        }
        for kind, chat in cases.items():
            with self.subTest(kind):
                got = self.read(self.say(chat, f"hello from {kind}"))
                self.assertEqual((got.chat_type, got.text), (kind, f"hello from {kind}"))

    def test_a_direct_message_is_named_after_the_other_person_as_the_app_names_it(self):
        chat = self.direct(self.a_owner, self.a_editor)
        self.assertEqual(self.read(self.say(chat, "hi")).chat_title, "a_editor")

    def test_a_thread_reply_reads_and_names_its_thread(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        root = self.say(chat, "root")
        reply = self.say(chat, "the answer is 42", reply_to=root)
        got = self.read(reply)
        self.assertEqual((got.text, got.thread_id), ("the answer is 42", str(root.id)))
        self.assertIsNone(self.read(root).thread_id)

    def test_the_text_is_the_body_not_the_client_derived_preview(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        message = self.say(chat, "the real words")
        Message.objects.filter(pk=message.pk).update(body_text="something else the client wrote")
        self.assertEqual(self.read(message).text, "the real words")

    def test_a_message_with_no_sender_and_no_text_still_reads(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        message = self.say(chat, "", empty=True)
        Message.objects.filter(pk=message.pk).update(sender=None)
        got = self.read(message)
        self.assertEqual((got.sender, got.text), ("", ""))

    def test_a_long_message_is_cut_at_the_cap_and_says_so(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        got = self.read(self.say(chat, "x" * (MAX_TEXT_CHARS + 50)))
        self.assertEqual((len(got.text), got.truncated), (MAX_TEXT_CHARS, True))

    def test_the_text_is_cleaned_of_what_a_person_cannot_see(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        got = self.read(self.say(chat, "a\u202eb\u200bc\ue000d"))
        self.assertEqual(got.text, "abcd")

    def test_attachments_and_replies_are_counted_not_read(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        root = self.say(chat, "root")
        self.say(chat, "r1", reply_to=root)
        Message.objects.filter(pk=root.pk).update(reply_count=1)
        self.assertEqual(self.read(root).replies, 1)

    def test_a_guest_team_reads_the_chat_shared_with_it_in_its_own_team_only(self):
        external, grant = self.shared_chat()
        message = self.say(external, "shared room talk", sender=self.a_owner)
        add_external_participants(grant, [self.b_owner.id], self.b_owner)
        self.assertEqual(
            self.read(message, user=self.b_owner, team=self.team_b).text, "shared room talk"
        )
        # The same person asking in the stranger team's name reads nothing.
        self.assertIsNone(self.read(message, user=self.b_owner, team=self.team_c))

    def test_the_hosts_project_chat_is_read_through_the_project_by_a_guest_it_was_shared_with(self):
        self.active_project_grant()
        pm = self.project_chat()
        message = self.say(pm, "host project talk", sender=self.a_owner)
        self.assertEqual(
            self.read(message, user=self.b_owner, team=self.team_b).text, "host project talk"
        )

    # -- what they cannot, one way each --------------------------------------

    def test_every_way_of_being_refused_reads_nothing(self):
        for name, message_id in self.unreadable().items():
            with self.subTest(name):
                self.assertIsNone(self.read(message_id), name)

    def test_the_same_messages_do_read_for_someone_who_may(self):
        """The refusals above mean something only beside this: the rows are real and read."""
        mine = self.group("Mine", [self.a_owner, self.a_editor])
        message = self.say(mine, "real words")
        self.assertEqual(self.read(message).text, "real words")

    def test_a_message_deleted_after_it_was_readable_stops_reading(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        message = self.say(chat, "now you see it")
        self.assertIsNotNone(self.read(message))
        Message.objects.filter(pk=message.pk).update(deleted_at=timezone.now())
        self.assertIsNone(self.read(message))

    def test_leaving_the_chat_stops_reading_its_messages(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        message = self.say(chat, "now you see it")
        self.assertIsNotNone(self.read(message))
        ChannelMember.objects.filter(channel=chat, user=self.a_owner).update(is_deleted=True)
        self.assertIsNone(self.read(message))

    def test_a_project_chat_is_read_by_the_project_not_by_a_channel_row(self):
        pm = self.project_chat(members=[self.a_editor])
        message = self.say(pm, "project talk")
        ChannelMember.objects.update_or_create(
            channel=pm, user=self.a_owner, defaults={"is_deleted": False}
        )
        self.assertIsNone(self.read(message))
        self.project_chat(members=[self.a_owner])
        self.assertEqual(self.read(message).text, "project talk")

    def test_the_history_window_hides_older_messages_and_only_those(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        old = self.say(chat, "old", at=timezone.now() - timedelta(days=40))
        new = self.say(chat, "new", at=timezone.now() - timedelta(days=2))
        with self.with_retention(30):
            self.assertIsNone(self.read(old))
            self.assertEqual(self.read(new).text, "new")
        with self.with_retention(None):
            self.assertEqual(self.read(old).text, "old")

    def test_a_context_with_no_user_reads_nothing(self):
        chat = self.group("G", [self.a_owner, self.a_editor])
        message = self.say(chat, "x")
        self.assertIsNone(read_message(str(message.id), str(self.team_a.team_id), ""))
        self.assertIsNone(read_message(str(message.id), str(self.team_a.team_id), None))

    # -- missing and refused are the same work -------------------------------

    def test_a_missing_message_and_every_refusal_cost_the_same_queries(self):
        ids = self.unreadable()
        self.read(MISSING)  # warm the caches either path may fill once
        counts = {}
        for name, message_id in ids.items():
            with CaptureQueriesContext(connection) as queries:
                self.read(message_id)
            counts[name] = len(queries)
        self.assertEqual(len(set(counts.values())), 1, counts)

    def test_the_lookup_is_one_query_for_the_message_whatever_the_answer(self):
        ids = self.unreadable()
        self.read(MISSING)
        for name, message_id in ids.items():
            with self.subTest(name), CaptureQueriesContext(connection) as queries:
                self.read(message_id)
            lookups = [q["sql"] for q in queries if q["sql"].startswith('SELECT "origin_message"')]
            self.assertEqual(len(lookups), 1, (name, lookups))


class MentionResolveTests(AskRefWorld):
    """The same rule through the mention path, as `/agent/ask/` calls it."""

    def resolve(self, entries, user=None):
        ctx = ToolContext(team_id=str(self.team_a.team_id), user_id=str((user or self.a_owner).id))
        return resolve_mentions(parse_mentions(entries), ctx)

    def test_a_readable_message_resolves_to_ids_and_no_label(self):
        chat = self.group("Roadmap", [self.a_owner, self.a_editor])
        message = self.say(chat, "Friday works")
        (m,) = self.resolve([{"type": "message", "message_id": str(message.id), "label": "spoof"}])
        self.assertEqual(
            (m.kind, m.label, m.message_id, m.message_chat_type_label, m.message_chat_id),
            ("message", "", str(message.id), "gm", str(chat.id)),
        )
        self.assertNotIn("Friday", str(m.as_json()))
        self.assertNotIn("Friday", repr(m))

    def test_every_refusal_drops_the_entry_without_an_error(self):
        for name, message_id in self.unreadable().items():
            with self.subTest(name):
                self.assertEqual(self.resolve([{"type": "message", "message_id": message_id}]), [])


class AskTwinTests(AskRefWorld):
    """A message that is not there and one the asker may not read, end to end through the view."""

    def observe(self, message_id):
        with (
            self.assertLogs("origin", level=logging.DEBUG) as logs,
            CaptureQueriesContext(connection) as queries,
        ):
            logging.getLogger("origin").debug("anchor")  # assertLogs fails on no records at all
            resp, captured = self.ask(
                mentions=[{"type": "message", "message_id": message_id, "label": "Secret thread"}]
            )
        run = AgentRun.objects.latest("started_at")
        return {
            "status": resp.status_code,
            "system_extra": captured["kwargs"]["system_extra"],
            "seed_sources": captured["kwargs"]["seed_sources"],
            "resolved_mentions": captured["ctx"].resolved_mentions,
            "run_mentions": run.mentions,
            "dropped": captured["stream"]["dropped_references"],
            "logs": [normalised(line) for line in logs.output],
            "queries": len(queries),
        }

    def test_nothing_observable_tells_a_missing_message_from_a_refused_one(self):
        ids = self.unreadable("SECRET-WORDS-123")
        self.observe(MISSING)  # warm
        baseline = self.observe(MISSING)
        self.assertEqual(baseline["status"], 200)
        self.assertEqual(baseline["dropped"], ["message"])
        for name, message_id in ids.items():
            with self.subTest(name):
                self.assertEqual(self.observe(message_id), baseline)

    def test_no_refused_text_reaches_the_prompt_the_logs_or_the_row(self):
        for name, message_id in self.unreadable("SECRET-WORDS-123").items():
            with self.subTest(name):
                seen = self.observe(message_id)
                self.assertNotIn("SECRET-WORDS-123", repr(seen))
                self.assertNotIn("Secret thread", repr(seen))  # the client's label, either

    def test_the_same_ask_for_a_readable_message_does_carry_its_text(self):
        chat = self.group("Mine", [self.a_owner, self.a_editor])
        message = self.say(chat, "SECRET-WORDS-123")
        seen = self.observe(str(message.id))
        self.assertIn("SECRET-WORDS-123", seen["system_extra"])
        self.assertEqual(seen["dropped"], [])
        self.assertNotIn("SECRET-WORDS-123", repr(seen["run_mentions"]))
        self.assertNotIn("SECRET-WORDS-123", repr(seen["logs"]))
        self.assertNotIn("Secret thread", repr(seen))

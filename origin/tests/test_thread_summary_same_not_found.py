"""MIP-1519: the thread summary surfaces answer a chat you cannot read like a chat that is not there.

`POST /api/v2/agent/thread-summary/` (and the thread branch of `/agent/ask/`, which shares the
loader) said "Chat not found or has no members." for a chat id that is not there but "Not
authorized to read this thread." for one that is, so a signed-in user could walk the id space and
learn which chats exist. The same fix the note summary got (MIP-1516): one answer, with the same
words, for "missing", "exists, you are not in it" and "no members". The status is unchanged (403,
as it was for both), so nothing the page does with it changes. Nobody gains access.
"""

from __future__ import annotations

import uuid
from unittest import mock

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.search_engine.agent import controller
from origin.tests.test_agent_non_object_body import _AgentClient

SUMMARY = "/api/v2/agent/thread-summary/"
ASK = "/api/v2/agent/ask/"
NOT_FOUND = {"error": "Chat not found or has no members."}


class Base(_AgentClient):
    """`self.user` is the only member of a group chat; `self.user2` is a teammate outside it."""

    def setUp(self):
        super().setUp()
        # No model is ever reached: the loader refuses first, and the agent loop has no client.
        client = mock.patch.object(
            controller, "get_model_client", side_effect=RuntimeError("no model")
        )
        client.start()
        self.addCleanup(client.stop)
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.thread = uuid.uuid4()

    def thread_args(self, chat_id):
        return {
            "chat_type": int(ChannelKind.GM),
            "chat_id": str(chat_id),
            "thread_id": str(self.thread),
        }

    def summary(self, user, chat_id):
        self.authenticate(user)
        return self.client.post(
            SUMMARY, {"team_id": str(self.team.team_id), **self.thread_args(chat_id)}, format="json"
        )

    def ask(self, user, chat_id):
        self.authenticate(user)
        return self.client.post(
            ASK,
            {
                "query": "what was decided?",
                "team_id": str(self.team.team_id),
                "thread_context": self.thread_args(chat_id),
            },
            format="json",
        )

    def assertSameAnswer(self, existing, missing):
        self.assertEqual(missing.status_code, 403, missing.content)
        self.assertEqual(missing.json(), NOT_FOUND)
        self.assertEqual(existing.status_code, missing.status_code, existing.content)
        self.assertEqual(existing.json(), missing.json())


class ThreadSummaryView(Base):
    def test_a_chat_you_are_not_in_gets_what_a_missing_chat_gets(self):
        self.assertSameAnswer(
            self.summary(self.user2, self.channel.id), self.summary(self.user2, uuid.uuid4())
        )

    def test_a_member_is_not_told_the_chat_is_missing(self):
        res = self.summary(self.user, self.channel.id)
        self.assertNotEqual(res.json(), NOT_FOUND)
        self.assertNotIn("authorized", res.content.decode())


class ThreadBranchOfAsk(Base):
    def test_a_chat_you_are_not_in_gets_what_a_missing_chat_gets(self):
        self.assertSameAnswer(
            self.ask(self.user2, self.channel.id), self.ask(self.user2, uuid.uuid4())
        )

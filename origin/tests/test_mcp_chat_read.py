"""Over MCP, chats and threads can be found and read, and only read (MIP-885).

`list_my_chats`, `get_chat_thread` (the registry's `fetch_chat_thread`),
`list_my_mentions` and `list_channel_members` are published. The tool-level rules
are in `test_agent_tool_chat_read`; this is what the endpoint adds: the names
and descriptions, ids that arrive from the other reads, a chat named by its
URL, a key bound to one team, and that no chat write is published.
"""

from __future__ import annotations

import re

from django.db.models import Max

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools import REGISTRY
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CHAT_READS = {"list_my_chats", "get_chat_thread", "list_my_mentions", "list_channel_members"}


class McpChatBase(McpBase):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Design crew")
        for user in (self.user, self.user2):
            ChannelMember.objects.create(channel=self.gm, user=user)
        self._key(scope=SCOPE_READ)

    def say(self, channel, text, sender=None, reply_to=None):
        seq = (Message.objects.filter(channel=channel).aggregate(m=Max("seq"))["m"] or 0) + 1
        return Message.objects.create(
            channel=channel,
            sender=sender or self.user2,
            seq=seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            thread_root=reply_to,
            parent=reply_to,
            is_thread_reply=reply_to is not None,
        )

    def call(self, name, arguments=None, **kw):
        result = self._result(self._call(name, arguments or {}, **kw))
        return result, result["content"][0]["text"]

    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class TestChatReadsAreOnTheCatalog(McpChatBase):
    def test_a_read_key_sees_the_four_reads(self):
        self.assertLessEqual(CHAT_READS, set(self.listed()))

    def test_the_registry_name_is_published_under_the_get_name(self):
        self.assertIn("get_chat_thread", tools.BY_NAME)
        self.assertEqual(tools.BY_NAME["get_chat_thread"].registry_name, "fetch_chat_thread")
        self.assertNotIn("fetch_chat_thread", tools.BY_NAME)
        # And a description that points at it uses the published name.
        self.assertIn("get_chat_thread", self.listed()["list_my_chats"]["description"])
        for t in self.listed().values():
            self.assertNotIn("fetch_chat_thread", str(t))

    def test_every_chat_read_is_read_only(self):
        for name in CHAT_READS:
            tool = tools.BY_NAME[name]
            self.assertFalse(tool.write, name)
            self.assertFalse(REGISTRY[tool.registry_name].requires_approval, name)
            self.assertTrue(tools.annotations(tool)["readOnlyHint"], name)

    def test_no_chat_write_is_published_or_built(self):
        """Chat is read-only for now: nothing that posts, edits or deletes a message."""
        word = re.compile(r"chat|message|channel|thread|mention|dm\b", re.I)
        self.assertEqual([t.name for t in tools.CATALOG if t.write and word.search(t.name)], [])
        self.assertEqual(
            [n for n, t in REGISTRY.items() if t.requires_approval and word.search(n)], []
        )

    def test_the_published_arguments_say_what_they_take(self):
        listed = self.listed()
        get = listed["get_chat_thread"]["inputSchema"]
        self.assertEqual(get["required"], ["chat_id"])
        for name in ("chat_type", "chat_id", "thread_id", "before", "limit"):
            self.assertIn(name, get["properties"])
        self.assertEqual(get["properties"]["limit"]["type"], "integer")
        mine = listed["list_my_chats"]["inputSchema"]
        self.assertEqual(set(mine["properties"]), {"query", "chat_type", "limit", "cursor"})
        self.assertEqual(mine["properties"]["chat_type"]["enum"], ["dm", "gm", "mdm", "pm"])


class TestReadingChatsOverMcp(McpChatBase):
    def test_a_chat_is_found_by_name_and_then_read(self):
        self.say(self.gm, "ship it on friday")

        found, text = self.call("list_my_chats", {"query": "CREW"})
        rows = found["structuredContent"]["chats"]
        self.assertEqual([r["title"] for r in rows], ["Design crew"])
        self.assertTrue(text.startswith("[Untrusted content"), "a read leads with the notice")

        row = rows[0]
        read, text = self.call(
            "get_chat_thread", {"chat_type": row["chat_type"], "chat_id": row["chat_id"]}
        )
        self.assertFalse(read["isError"], text)
        [message] = read["structuredContent"]["messages"]
        self.assertEqual(message["text"], "ship it on friday", "the in-app fence is taken off")
        self.assertEqual(message["sender_username"], "otheruser")
        self.assertNotIn("<workspace_content>", text)
        self.assertTrue(read["_meta"]["genos/untrustedContent"])

    def test_paging_back_through_a_long_chat(self):
        for i in range(1, 8):
            self.say(self.gm, f"m{i}")

        first, _ = self.call("get_chat_thread", {"chat_id": str(self.gm.id), "limit": 3})
        body = first["structuredContent"]
        self.assertEqual([m["text"] for m in body["messages"]], ["m5", "m6", "m7"])
        self.assertTrue(body["has_more"])
        second, _ = self.call(
            "get_chat_thread",
            {"chat_id": str(self.gm.id), "limit": 3, "before": body["next_cursor"]},
        )
        self.assertEqual(
            [m["text"] for m in second["structuredContent"]["messages"]], ["m2", "m3", "m4"]
        )

    def test_the_ids_of_a_search_hit_are_accepted(self):
        """What `search_workspace` returns for a chat hit: `chat_type`, `chat_id`
        and, for a message in a thread, `thread_id` (null in the main channel)."""
        root = self.say(self.gm, "the question")
        self.say(self.gm, "the answer", reply_to=root)

        main_hit = {"chat_type": "gm", "chat_id": str(self.gm.id), "thread_id": None}
        thread_hit = {"chat_type": "gm", "chat_id": str(self.gm.id), "thread_id": str(root.id)}

        main, _ = self.call("get_chat_thread", main_hit)
        thread, _ = self.call("get_chat_thread", thread_hit)

        self.assertFalse(main["isError"])
        self.assertEqual(
            [m["text"] for m in thread["structuredContent"]["messages"]],
            ["the question", "the answer"],
        )

    def test_the_ids_of_a_mention_are_accepted(self):
        root = self.say(self.gm, "plan?")
        reply = self.say(self.gm, "hey @you, look", reply_to=root)
        top = self.say(self.gm, "@you again")
        MessageMention.objects.create(message=reply, mentioned_user=self.user)
        MessageMention.objects.create(message=top, mentioned_user=self.user)

        result, text = self.call("list_my_mentions")
        rows = result["structuredContent"]["mentions"]
        self.assertEqual(len(rows), 2)

        for row in rows:
            with self.subTest(thread_id=row["thread_id"]):
                args = {"chat_type": row["chat_type_label"], "chat_id": row["chat_id"]}
                if row["thread_id"]:
                    args["thread_id"] = row["thread_id"]
                read, text = self.call("get_chat_thread", args)
                self.assertFalse(read["isError"], text)
                texts = [m["text"] for m in read["structuredContent"]["messages"]]
                self.assertIn(row["text"], texts)

    def test_a_chat_url_names_the_chat(self):
        root = self.say(self.gm, "the question")
        self.say(self.gm, "the answer", reply_to=root)
        url = f"https://app.genosai.dev/workspace/chat/gm/{self.gm.id}/thread/{root.id}"

        read, text = self.call("get_chat_thread", {"chat_id": url})

        self.assertFalse(read["isError"], text)
        self.assertEqual(read["structuredContent"]["thread_id"], str(root.id))
        self.assertEqual(len(read["structuredContent"]["messages"]), 2)

    def test_a_chat_the_caller_is_not_in_reads_like_one_that_is_not_there(self):
        theirs = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Theirs")
        ChannelMember.objects.create(channel=theirs, user=self.user2)
        self.say(theirs, "secret")

        refused, refused_text = self.call("get_chat_thread", {"chat_id": str(theirs.id)})
        missing, missing_text = self.call(
            "get_chat_thread", {"chat_id": "11111111-2222-4333-8444-555555555555"}
        )

        self.assertTrue(refused["isError"])
        self.assertTrue(missing["isError"])
        self.assertEqual(_UUID.sub("<id>", refused_text), _UUID.sub("<id>", missing_text))
        self.assertNotIn("secret", refused_text)

    def test_the_roster_of_a_chat_the_caller_is_not_in_reads_the_same(self):
        theirs = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Theirs")
        ChannelMember.objects.create(channel=theirs, user=self.user2)

        refused, refused_text = self.call("list_channel_members", {"channel_id": str(theirs.id)})
        missing, missing_text = self.call(
            "list_channel_members", {"channel_id": "11111111-2222-4333-8444-555555555555"}
        )
        mine, mine_text = self.call("list_channel_members", {"channel_id": str(self.gm.id)})

        self.assertTrue(refused["isError"] and missing["isError"])
        self.assertEqual(_UUID.sub("<id>", refused_text), _UUID.sub("<id>", missing_text))
        self.assertFalse(mine["isError"], mine_text)
        self.assertEqual(
            {m["username"] for m in mine["structuredContent"]["members"]}, {"testuser", "otheruser"}
        )

    def test_a_key_for_one_team_does_not_reach_the_same_persons_chats_in_another(self):
        other = TeamMaster.objects.create(team_name="Contractors", owner=self.user2)
        TeamMembers.objects.create(team=other, attendee=self.user)
        elsewhere = Channel.objects.create(team=other, kind=ChannelKind.GM, title="Elsewhere")
        ChannelMember.objects.create(channel=elsewhere, user=self.user)
        self.say(elsewhere, "from the other team")

        self._key(scope=SCOPE_READ, team=self.team)
        listed, _ = self.call("list_my_chats", url=self._url(team=False))
        titles = {r["title"] for r in listed["structuredContent"]["chats"]}
        refused, text = self.call(
            "get_chat_thread", {"chat_id": str(elsewhere.id)}, url=self._url(team=False)
        )

        # (The project chat of the fixture project is in the list too.)
        self.assertEqual(titles, {"Design crew", "Website Redesign"})
        self.assertTrue(refused["isError"])
        self.assertNotIn("from the other team", text)

        self._key(scope=SCOPE_READ, team=other)
        there, _ = self.call("list_my_chats", url=self._url(team=False))
        self.assertEqual({r["title"] for r in there["structuredContent"]["chats"]}, {"Elsewhere"})
        read, text = self.call(
            "get_chat_thread", {"chat_id": str(elsewhere.id)}, url=self._url(team=False)
        )
        self.assertFalse(read["isError"], text)

    def test_a_misspelt_argument_is_refused_not_ignored(self):
        """A read that ignored `chat_tpye` would list or read the wrong thing and
        look authoritative (MIP-763)."""
        result, text = self.call("list_my_chats", {"chat_tpye": "dm"})

        self.assertTrue(result["isError"])
        self.assertIn("chat_type", text)

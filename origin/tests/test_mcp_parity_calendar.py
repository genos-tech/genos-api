"""The calendar's several Google accounts and `get_calendar_event`, over MCP (MIP-1059, `AGENT_PARITY_TOOLS`).

The MCP catalog serves the same registry tools the app's agent runs, so what the flag does there is pinned
separately: the new tool is listed only while the flag is on, `account` is in the schemas only while it is on,
a handle that is not the key owner's own account is refused with the one sentence and nothing is sent to
Google, and a client is never given a citation token it has no grammar for.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.search_engine.agent.calendar import ACCOUNT_REFUSAL
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools.create_calendar_event import (
    DESCRIPTION_ADDITION as CREATE_ACCOUNT_HINT,
)
from origin.views.public.mcp import tools as mcp

from .calendar_fakes import FakeGoogle, connect_google, token_for
from .test_mcp_endpoint import MODERN, McpBase

ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}
CALENDAR_TOOLS = (
    "list_calendars",
    "list_calendar_events",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
)
CREATE = {
    "summary": "Design review",
    "start_iso": "2026-12-03T10:00:00",
    "end_iso": "2026-12-03T11:00:00",
    "time_zone": "Asia/Tokyo",
}


class CalendarMcpBase(McpBase):
    flag = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON if self.flag else OFF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = connect_google(self.user, "sub-work", "me@work.example", login=True)
        self.home = connect_google(self.user, "sub-home", "me@home.example")
        self.stranger = connect_google(self.user2, "sub-x", "you@elsewhere.example", login=True)
        self.google = FakeGoogle()
        patched = self.google.patched()
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.google.add_event(self.work, "primary", "wk1", summary="Standup")
        self.google.add_event(self.home, "primary", "hm1", summary="Dentist")
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        self.google.add_calendar(self.home, "me@home.example", primary=True)

    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class FlagOnTests(CalendarMcpBase):
    def test_the_new_tool_is_listed_read_only_and_every_calendar_tool_takes_an_account(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self.listed()
        self.assertIn("get_calendar_event", listed)
        self.assertEqual(
            listed["get_calendar_event"]["annotations"],
            {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        )
        for name in (*CALENDAR_TOOLS, "get_calendar_event"):
            with self.subTest(tool=name):
                prop = listed[name]["inputSchema"]["properties"]["account"]
                self.assertEqual(prop["type"], "string")
                self.assertNotIn("account", listed[name]["inputSchema"].get("required", []))

    def test_a_read_only_key_reads_an_event_on_the_second_account(self):
        self._key(scope=SCOPE_READ)
        self.assertIn("get_calendar_event", self.listed())
        text, is_error = self._tool_text(
            self._call("get_calendar_event", {"event_id": "hm1", "account": str(self.home.id)})
        )
        self.assertFalse(is_error, text)
        self.assertIn("Dentist", text)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.home)])
        self.assertIn(str(self.home.id), text)

    def test_the_result_has_no_citation_token_for_a_client_with_no_grammar_for_one(self):
        self._key(scope=SCOPE_READ)
        text, _ = self._tool_text(self._call("get_calendar_event", {"event_id": "wk1"}))
        self.assertNotIn("calendar_event:", text)
        self.assertNotIn('"cite"', text)
        listed, _ = self._tool_text(self._call("list_calendar_events", {}))
        self.assertNotIn('"cite"', listed)
        self.assertIn(str(self.work.id), listed)

    def test_another_members_account_is_refused_and_nothing_is_sent(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        for name, args in (
            ("get_calendar_event", {"event_id": "e"}),
            ("list_calendar_events", {}),
            ("list_calendars", {}),
            ("create_calendar_event", CREATE),
            ("update_calendar_event", {"event_id": "e", "summary": "x"}),
            ("delete_calendar_event", {"event_id": "e"}),
        ):
            with self.subTest(tool=name):
                text, is_error = self._tool_text(
                    self._call(name, {**args, "account": str(self.stranger.id)})
                )
                self.assertTrue(is_error)
                self.assertEqual(text.split("\n")[-1] if "\n" in text else text, ACCOUNT_REFUSAL)
        self.assertEqual(self.google.calls, [])

    def test_the_refusal_is_the_same_for_a_made_up_id(self):
        self._key(scope=SCOPE_READ)
        mine, _ = self._tool_text(
            self._call("list_calendar_events", {"account": str(self.stranger.id)})
        )
        made_up, _ = self._tool_text(
            self._call("list_calendar_events", {"account": "11111111-1111-4111-8111-111111111111"})
        )
        self.assertEqual(mine, made_up)

    def test_a_write_key_creates_on_the_second_account(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(
            self._call("create_calendar_event", {**CREATE, "account": str(self.home.id)})
        )
        self.assertFalse(is_error, text)
        (post,) = self.google.of("POST")
        self.assertEqual(post.token, token_for(self.home))
        self.assertIn(str(self.home.id), text)

    def test_a_delete_on_the_second_account_still_needs_the_destructive_opt_in(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(
            self._call(
                "delete_calendar_event",
                {"event_id": "hm1", "account": str(self.home.id)},
                era=MODERN,
            )
        )
        self.assertTrue(is_error)
        self.assertEqual(self.google.of("DELETE"), [])
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text, is_error = self._tool_text(
            self._call("delete_calendar_event", {"event_id": "hm1", "account": str(self.home.id)})
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.google.of("DELETE")[0].token, token_for(self.home))

    def test_a_read_only_key_cannot_write_to_any_account(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(
            self._call("create_calendar_event", {**CREATE, "account": str(self.home.id)})
        )
        self.assertTrue(is_error)
        self.assertEqual(self.google.calls, [])

    def test_list_calendars_lists_every_account(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(self._call("list_calendars", {}))
        self.assertFalse(is_error, text)
        payload = json.loads(text[text.index("{") :])
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual(
            {c["account_id"] for c in payload["calendars"]}, {str(self.work.id), str(self.home.id)}
        )

    def test_the_descriptions_say_the_account_changes(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self.listed()
        self.assertIn("EVERY connected Google account", listed["list_calendars"]["description"])
        self.assertIn("ONE account", listed["list_calendar_events"]["description"])
        self.assertIn("already gone", listed["delete_calendar_event"]["description"])
        # The in-app sentence that sends a named account through `list_calendars` is the MCP one too, on create only.
        self.assertTrue(
            listed["create_calendar_event"]["description"].endswith(
                "Requires a write-scoped API key." + CREATE_ACCOUNT_HINT
            )
        )
        for name in ("update_calendar_event", "delete_calendar_event", "list_calendar_events"):
            self.assertNotIn(CREATE_ACCOUNT_HINT, listed[name]["description"], name)
        self.assertIn("account", listed["get_calendar_event"]["description"])
        self.assertNotIn("cite", listed["get_calendar_event"]["description"])

    def test_the_new_tool_leads_its_result_with_the_untrusted_notice_like_every_read(self):
        self._key(scope=SCOPE_READ)
        text, _ = self._tool_text(self._call("get_calendar_event", {"event_id": "wk1"}))
        self.assertTrue(text.startswith(mcp.UNTRUSTED_NOTICE.split("\n")[0][:40]), text[:80])


class FlagOffTests(CalendarMcpBase):
    flag = False

    def test_the_new_tool_is_not_listed_and_a_call_is_answered_as_for_an_unknown_name(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertNotIn("get_calendar_event", self.listed())
        res = self._call("get_calendar_event", {"event_id": "wk1"})
        self.assertIn("error", res.data)
        self.assertEqual(self.google.calls, [])

    def test_no_calendar_schema_has_an_account(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self.listed()
        for name in CALENDAR_TOOLS:
            self.assertNotIn("account", listed[name]["inputSchema"]["properties"], name)
            self.assertNotIn("account", listed[name]["description"].split("Requires")[0][-60:])

    def test_no_description_carries_the_account_hint(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        for name, tool in self.listed().items():
            with self.subTest(tool=name):
                self.assertNotIn(CREATE_ACCOUNT_HINT, tool["description"])
                self.assertNotIn("names a specific calendar or account", tool["description"])

    def test_an_account_argument_is_refused_as_unknown_so_nothing_is_sent(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        for name, args in (
            ("list_calendar_events", {}),
            ("create_calendar_event", CREATE),
            ("delete_calendar_event", {"event_id": "hm1"}),
        ):
            with self.subTest(tool=name):
                text, is_error = self._tool_text(
                    self._call(name, {**args, "account": str(self.home.id)})
                )
                self.assertTrue(is_error)
                self.assertIn("account", text)
        self.assertEqual(self.google.calls, [])

    def test_list_calendars_reads_the_first_account_only_as_before(self):
        self._key(scope=SCOPE_READ)
        text, _ = self._tool_text(self._call("list_calendars", {}))
        payload = json.loads(text[text.index("{") :])
        self.assertNotIn("accounts", payload)
        self.assertEqual([c["id"] for c in payload["calendars"]], ["me@work.example"])

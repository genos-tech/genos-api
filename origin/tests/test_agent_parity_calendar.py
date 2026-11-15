"""Several Google accounts for the calendar tools, and `get_calendar_event` (MIP-1059, `AGENT_PARITY_TOOLS`).

The app lets a member connect several Google accounts (api #205) and addresses an event by the triple
account / calendar / event. The agent's calendar tools reached only the FIRST account, so an event on the
second was unreachable. With the flag on:

  * the five calendar tools take `account`, the `account_id` `list_calendars` returns, and a sixth tool
    reads one event;
  * a handle that is not the member's own account is refused in ONE sentence that does not tell a
    made-up id from another member's, and nothing is sent to Google;
  * omitted, `account` is exactly what it always was: the first account.

With the flag off nothing the model or an MCP client is shown moves, and a write that arrives naming an
account is refused rather than quietly sent to the default one (an approval paused while the flag was on).

Google is a fake (`calendar_fakes.FakeGoogle`): every call is recorded with the token of the account it
was made with, so "the second account was read" is a fact about the call and not about a result.
"""

from __future__ import annotations

import json
import uuid
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent import controller, prompts, tool_flag_params, tool_flag_text
from origin.search_engine.agent.calendar import (
    ACCOUNT_REFUSAL,
    event_cite,
    named_google_account,
    resolve_google_calendar_account,
)
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT, agent_system_prompt
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_calendar_event import (
    CREATE_CALENDAR_EVENT,
)
from origin.search_engine.agent.tools.create_calendar_event import (
    DESCRIPTION_ADDITION as CREATE_ACCOUNT_HINT,
)
from origin.search_engine.agent.tools.delete_calendar_event import DELETE_CALENDAR_EVENT
from origin.search_engine.agent.tools.get_calendar_event import GET_CALENDAR_EVENT
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.search_engine.agent.tools.list_calendars import LIST_CALENDARS
from origin.search_engine.agent.tools.update_calendar_event import UPDATE_CALENDAR_EVENT

from .calendar_fakes import FakeGoogle, connect_google, token_for

User = get_user_model()

ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}
CALENDAR_TOOLS = (
    "list_calendars",
    "list_calendar_events",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
)
WITH_ACCOUNT = CALENDAR_TOOLS  # every one of them takes `account` while the flag is on
HOLIDAY = "en.japanese#holiday@group.v.calendar.google.com"
START = {"dateTime": "2026-12-01T10:00:00+09:00", "timeZone": "Asia/Tokyo"}
END = {"dateTime": "2026-12-01T11:00:00+09:00", "timeZone": "Asia/Tokyo"}
CREATE_ARGS = {
    "summary": "Design review",
    "start_iso": "2026-12-01T10:00:00",
    "end_iso": "2026-12-01T11:00:00",
    "time_zone": "Asia/Tokyo",
}


class CalendarBase(TestCase):
    """`self.user` has two Google accounts: `work` (the login identity, so the default) and `home`.
    `self.other` is another member with one account, `stranger`."""

    flag = True

    def setUp(self):
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON if self.flag else OFF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(
            username="me", email="me@x.example", password="pw12345!"
        )
        self.other = User.objects.create_user(
            username="you", email="you@x.example", password="pw12345!"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@x.example", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        TeamMembers.objects.create(team=self.team, attendee=self.other)
        self.work = connect_google(self.user, "sub-work", "me@work.example", login=True)
        self.home = connect_google(self.user, "sub-home", "me@home.example")
        self.stranger = connect_google(
            self.other, "sub-stranger", "you@elsewhere.example", login=True
        )
        self.google = FakeGoogle()
        patched = self.google.patched()
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def calls_made_with(self, account):
        return [c for c in self.google.calls if c.token == token_for(account)]


# --------------------------------------------------------------------------- #
# The ownership guard: a member reaches their OWN accounts and no one else's   #
# --------------------------------------------------------------------------- #


class AccountGuardTests(CalendarBase):
    def test_omitted_is_the_first_account_the_login_identity(self):
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.work)])
        self.assertEqual(out["account_id"], str(self.work.id))

    def test_omitted_is_exactly_what_it_was_before_the_parameter_existed(self):
        # The same row the unfiltered query returns, which is what the tools always used.
        first = type(self.work).objects.filter(user=self.user, provider="google").first()
        self.assertEqual(resolve_google_calendar_account(str(self.user.id)).id, first.id)
        self.assertEqual(resolve_google_calendar_account(str(self.user.id), None).id, first.id)
        self.assertEqual(resolve_google_calendar_account(str(self.user.id), "").id, first.id)

    def test_the_default_is_the_oldest_when_none_is_the_login_identity(self):
        type(self.work).objects.filter(id=self.work.id).update(is_login_identity=False)
        self.assertEqual(resolve_google_calendar_account(str(self.user.id)).id, self.work.id)

    def test_a_named_account_is_the_one_called(self):
        out = LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.home)])
        self.assertEqual(out["account_id"], str(self.home.id))

    def test_the_id_is_read_however_it_is_spelled(self):
        for spelling in (
            str(self.home.id).upper(),
            str(self.home.id).replace("-", ""),
            f" {self.home.id} ",
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(named_google_account(self.user, spelling).id, self.home.id)

    def test_another_members_account_is_refused_and_nothing_is_sent(self):
        for tool, args in (
            (LIST_CALENDAR_EVENTS, {}),
            (GET_CALENDAR_EVENT, {"event_id": "e1"}),
            (CREATE_CALENDAR_EVENT, CREATE_ARGS),
            (UPDATE_CALENDAR_EVENT, {"event_id": "e1", "summary": "x"}),
            (DELETE_CALENDAR_EVENT, {"event_id": "e1"}),
            (LIST_CALENDARS, {}),
        ):
            with self.subTest(tool=tool.name):
                with self.assertRaises(ToolForbidden) as raised:
                    tool.run({**args, "account": str(self.stranger.id)}, self.ctx)
                self.assertEqual(str(raised.exception), ACCOUNT_REFUSAL)
        self.assertEqual(self.google.calls, [])

    def test_a_made_up_id_and_another_members_id_read_the_same(self):
        made_up = str(uuid.uuid4())
        with self.assertRaises(ToolError) as nothing:
            LIST_CALENDAR_EVENTS.run({"account": made_up}, self.ctx)
        with self.assertRaises(ToolError) as theirs:
            LIST_CALENDAR_EVENTS.run({"account": str(self.stranger.id)}, self.ctx)
        self.assertEqual(str(nothing.exception), str(theirs.exception))
        # Only the class differs (MCP counts "found but not yours" toward its refusal alarm).
        self.assertNotIsInstance(nothing.exception, ToolForbidden)
        self.assertIsInstance(theirs.exception, ToolForbidden)

    def test_a_value_that_is_not_an_id_is_refused_without_echoing_it(self):
        # The model copies what the member said: an address. A UUID column would raise a
        # `ValidationError` quoting it, and the controller logs a tool's crash.
        for value in ("me@work.example", "work", "primary", "1", "{}", "x" * 40):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as raised:
                    LIST_CALENDAR_EVENTS.run({"account": value}, self.ctx)
                self.assertEqual(str(raised.exception), ACCOUNT_REFUSAL)
                self.assertNotIn(value, str(raised.exception))
        self.assertEqual(self.google.calls, [])

    def test_the_refusal_names_the_way_out(self):
        self.assertIn("list_calendars", ACCOUNT_REFUSAL)
        self.assertIn("account_id", ACCOUNT_REFUSAL)
        self.assertIn("leave `account` out", ACCOUNT_REFUSAL)

    def test_a_member_with_no_google_account_is_told_so_not_that_the_id_is_wrong(self):
        lonely = User.objects.create_user(username="l", email="l@x.example", password="pw12345!")
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(lonely.id))
        with self.assertRaisesRegex(ToolError, "not connected"):
            LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, ctx)
        self.assertEqual(self.google.calls, [])

    def test_a_named_account_without_calendar_access_is_told_to_grant_it(self):
        bare = connect_google(self.user, "sub-bare", "me@bare.example", scopes=["openid"])
        with self.assertRaisesRegex(ToolError, "Calendar access"):
            LIST_CALENDAR_EVENTS.run({"account": str(bare.id)}, self.ctx)
        self.assertEqual(self.google.calls, [])

    def test_a_named_account_whose_sign_in_expired_is_told_to_reconnect(self):
        from origin.services.oauth.tokens import ReauthRequired  # noqa: PLC0415

        with mock.patch(
            "origin.search_engine.agent.calendar.get_valid_access_token",
            side_effect=ReauthRequired(self.home),
        ):
            with self.assertRaisesRegex(ToolError, "expired"):
                LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx)

    def test_an_id_that_is_not_a_google_account_of_the_member_is_refused(self):
        # A github row of the member's own: the id exists and is theirs, but it is not a Google account.
        gh = type(self.work).objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-1",
            access_token_encrypted="placeholder",
        )
        with self.assertRaises(ToolError) as raised:
            LIST_CALENDAR_EVENTS.run({"account": str(gh.id)}, self.ctx)
        self.assertEqual(str(raised.exception), ACCOUNT_REFUSAL)
        self.assertEqual(self.google.calls, [])

    def test_a_failing_google_logs_the_status_and_never_the_calendar_or_the_body(self):
        # The calendar is an address; the fake's error body and url quote it back, as Google's do.
        self.google.failing[token_for(self.home)] = 500
        address = "me@home.example"
        with self.assertLogs("origin.search_engine.agent.calendar", level="WARNING") as logged:
            with self.assertRaisesRegex(ToolError, "HTTP 500"):
                LIST_CALENDAR_EVENTS.run(
                    {"account": str(self.home.id), "calendar_id": address}, self.ctx
                )
        text = " ".join(logged.output)
        self.assertIn("status=500", text)
        for leak in (address, "me%40home.example", "Not found"):
            self.assertNotIn(leak, text)
        # list_calendars reports a failing account by its id and a status, never its address.
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        listed = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual([f["account_id"] for f in listed["failed_accounts"]], [str(self.home.id)])
        self.assertNotIn(address, str(listed["failed_accounts"]))

    def test_no_account_value_ever_reaches_a_url_or_a_log_line(self):
        with self.assertNoLogs(level="WARNING"):
            with self.assertRaises(ToolError):
                LIST_CALENDAR_EVENTS.run({"account": "me@work.example"}, self.ctx)


# --------------------------------------------------------------------------- #
# What a call reads and writes, and which account it is made with              #
# --------------------------------------------------------------------------- #


class CallTests(CalendarBase):
    def setUp(self):
        super().setUp()
        self.google.add_event(self.work, "primary", "wk1", summary="Standup")
        self.google.add_event(self.home, "primary", "hm1", summary="Dentist")

    def test_an_event_on_the_second_account_is_unreachable_by_default_and_reachable_by_name(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            GET_CALENDAR_EVENT.run({"event_id": "hm1"}, self.ctx)
        out = GET_CALENDAR_EVENT.run({"event_id": "hm1", "account": str(self.home.id)}, self.ctx)
        self.assertEqual(out["id"], "hm1")
        self.assertIn("Dentist", out["summary"])
        self.assertEqual(out["account_id"], str(self.home.id))
        self.assertEqual(out["account_label"], "me@home.example")

    def test_a_not_found_says_it_may_be_on_another_account(self):
        with self.assertRaises(ToolError) as raised:
            GET_CALENDAR_EVENT.run({"event_id": "hm1"}, self.ctx)
        self.assertIn("another one", str(raised.exception))
        self.assertIn("`account`", str(raised.exception))
        with self.assertRaises(ToolError) as updated:
            UPDATE_CALENDAR_EVENT.run({"event_id": "hm1", "summary": "x"}, self.ctx)
        self.assertIn("`account`", str(updated.exception))

    def test_list_reads_the_default_account_and_says_how_many_it_left_out(self):
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual([e["id"] for e in out["events"]], ["wk1"])
        self.assertEqual(out["other_accounts"], 1)
        self.assertEqual(out["account_label"], "me@work.example")

    def test_a_named_read_does_not_claim_to_have_left_anything_out(self):
        out = LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx)
        self.assertEqual([e["id"] for e in out["events"]], ["hm1"])
        self.assertNotIn("other_accounts", out)

    def test_a_member_with_one_account_never_hears_of_others(self):
        self.home.delete()
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertNotIn("other_accounts", out)
        self.assertEqual(out["account_id"], str(self.work.id))

    def test_create_on_the_second_account_goes_to_the_second_account(self):
        out = CREATE_CALENDAR_EVENT.run({**CREATE_ARGS, "account": str(self.home.id)}, self.ctx)
        (call,) = self.google.of("POST")
        self.assertEqual(call.token, token_for(self.home))
        self.assertEqual(call.path, "/calendars/primary/events")
        self.assertEqual(call.body["summary"], "Design review")
        self.assertEqual(out["account_id"], str(self.home.id))
        self.assertEqual(
            [k[0] for k in self.google.events if k[2] == out["id"]], [token_for(self.home)]
        )

    def test_create_with_no_account_is_the_first(self):
        out = CREATE_CALENDAR_EVENT.run(CREATE_ARGS, self.ctx)
        (call,) = self.google.of("POST")
        self.assertEqual(call.token, token_for(self.work))
        self.assertEqual(out["account_id"], str(self.work.id))

    def test_update_and_delete_on_the_second_account(self):
        updated = UPDATE_CALENDAR_EVENT.run(
            {"event_id": "hm1", "summary": "Dentist (moved)", "account": str(self.home.id)},
            self.ctx,
        )
        self.assertEqual(self.google.of("PATCH")[0].token, token_for(self.home))
        self.assertEqual(updated["changed_fields"], ["summary"])
        deleted = DELETE_CALENDAR_EVENT.run(
            {"event_id": "hm1", "account": str(self.home.id)}, self.ctx
        )
        self.assertEqual(self.google.of("DELETE")[0].token, token_for(self.home))
        self.assertTrue(deleted["deleted"] and not deleted["already_gone"])
        self.assertEqual(deleted["account_id"], str(self.home.id))
        self.assertNotIn((token_for(self.home), "primary", "hm1"), self.google.events)
        self.assertIn((token_for(self.work), "primary", "wk1"), self.google.events)

    def test_a_delete_on_the_wrong_account_reads_as_gone_and_says_which_account_it_checked(self):
        out = DELETE_CALENDAR_EVENT.run({"event_id": "hm1"}, self.ctx)
        self.assertTrue(out["already_gone"])
        self.assertEqual(out["account_id"], str(self.work.id))
        self.assertIn((token_for(self.home), "primary", "hm1"), self.google.events)

    def test_an_update_that_changes_nothing_names_no_account_and_calls_nothing(self):
        out = UPDATE_CALENDAR_EVENT.run({"event_id": "hm1", "account": str(self.home.id)}, self.ctx)
        self.assertEqual(out["changed_fields"], [])
        self.assertNotIn("account_id", out)
        self.assertEqual(self.google.calls, [])

    def test_every_event_row_carries_the_token_to_cite_it_by(self):
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        (row,) = out["events"]
        self.assertEqual(row["cite"], f"calendar_event:{self.work.id}:wk1")
        self.assertEqual(row["cite"], event_cite(self.work.id, "wk1"))
        single = GET_CALENDAR_EVENT.run({"event_id": "wk1"}, self.ctx)
        self.assertEqual(single["cite"], row["cite"])

    def test_mcp_is_not_given_a_citation_grammar_it_does_not_have(self):
        ctx = ToolContext(team_id=self.ctx.team_id, user_id=self.ctx.user_id, surface="mcp")
        out = LIST_CALENDAR_EVENTS.run({}, ctx)
        self.assertNotIn("cite", out["events"][0])
        self.assertEqual(out["account_id"], str(self.work.id))
        self.assertNotIn("cite", GET_CALENDAR_EVENT.run({"event_id": "wk1"}, ctx))

    def test_an_event_id_a_token_cannot_hold_gets_no_cite(self):
        self.google.add_event(self.work, "primary", "weird id!", summary="Odd")
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        row = [e for e in out["events"] if e["id"] == "weird id!"][0]
        self.assertNotIn("cite", row)
        self.assertIsNone(event_cite(self.work.id, "weird id!"))
        self.assertIsNone(event_cite(self.work.id, None))
        self.assertIsNone(event_cite("", "e1"))

    def test_a_recurring_instance_id_is_cited_as_it_is(self):
        self.assertEqual(
            event_cite(self.work.id, "abc123_20261201T010000Z"),
            f"calendar_event:{self.work.id}:abc123_20261201T010000Z",
        )


class PathTests(CalendarBase):
    """Ids are percent-encoded as the app's own endpoints do (`calendar_views._cal`)."""

    def test_a_holiday_calendar_keeps_its_hash_out_of_the_url_fragment(self):
        self.google.add_event(self.work, HOLIDAY, "h1", summary="Culture Day")
        out = LIST_CALENDAR_EVENTS.run({"calendar_id": HOLIDAY}, self.ctx)
        self.assertEqual([e["id"] for e in out["events"]], ["h1"])
        (call,) = self.google.calls
        self.assertIn("%23", call.raw_path)
        self.assertNotIn("#", call.raw_path)
        self.assertEqual(call.path, f"/calendars/{HOLIDAY}/events")

    def test_an_id_cannot_reach_another_endpoint_with_the_members_token(self):
        for tool, args in (
            (LIST_CALENDAR_EVENTS, {"calendar_id": "../users/me/calendarList"}),
            (GET_CALENDAR_EVENT, {"event_id": "x", "calendar_id": "primary/events?q=1#"}),
            (UPDATE_CALENDAR_EVENT, {"event_id": "../../settings/x", "summary": "s"}),
            (DELETE_CALENDAR_EVENT, {"event_id": "e/../..", "calendar_id": "a/b"}),
        ):
            with self.subTest(tool=tool.name):
                self.google.calls.clear()
                try:
                    tool.run(args, self.ctx)
                except ToolError:
                    pass
                (call,) = self.google.calls
                segments = call.raw_path.strip("/").split("/")
                self.assertEqual(segments[0], "calendars")
                self.assertEqual(segments[2], "events")
                self.assertNotIn("..", segments)
                self.assertLessEqual(len(segments), 4)

    def test_a_value_that_is_only_dots_is_refused_not_normalised_away(self):
        # "." and ".." survive percent-encoding, and a client would resolve them as path steps.
        for value in (".", "..", "..."):
            with self.subTest(value=value):
                with self.assertRaises(ToolError):
                    GET_CALENDAR_EVENT.run({"event_id": "e1", "calendar_id": value}, self.ctx)
                with self.assertRaises(ToolError):
                    GET_CALENDAR_EVENT.run({"event_id": value}, self.ctx)
        self.assertEqual(self.google.calls, [])

    def test_primary_is_untouched(self):
        LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual(self.google.calls[0].raw_path, "/calendars/primary/events")


# --------------------------------------------------------------------------- #
# list_calendars: every account, each tagged                                   #
# --------------------------------------------------------------------------- #


class ListCalendarsTests(CalendarBase):
    def setUp(self):
        super().setUp()
        self.google.add_calendar(self.work, "me@work.example", primary=True, accessRole="owner")
        self.google.add_calendar(self.work, "team@group.calendar.google.com", accessRole="reader")
        self.google.add_calendar(self.home, "me@home.example", primary=True, accessRole="owner")

    def test_it_lists_every_connected_account_each_calendar_tagged_with_its_account(self):
        out = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual(
            [(c["account_id"], c["id"]) for c in out["calendars"]],
            [
                (str(self.work.id), "me@work.example"),
                (str(self.work.id), "team@group.calendar.google.com"),
                (str(self.home.id), "me@home.example"),
            ],
        )
        self.assertEqual(
            out["accounts"],
            [
                {"account_id": str(self.work.id), "label": "me@work.example", "default": True},
                {"account_id": str(self.home.id), "label": "me@home.example", "default": False},
            ],
        )
        self.assertNotIn("failed_accounts", out)

    def test_the_default_account_is_the_one_every_other_tool_uses(self):
        (default,) = [a for a in LIST_CALENDARS.run({}, self.ctx)["accounts"] if a["default"]]
        self.assertEqual(
            default["account_id"], LIST_CALENDAR_EVENTS.run({}, self.ctx)["account_id"]
        )

    def test_named_it_lists_only_that_account(self):
        out = LIST_CALENDARS.run({"account": str(self.home.id)}, self.ctx)
        self.assertEqual([c["id"] for c in out["calendars"]], ["me@home.example"])
        self.assertEqual([a["account_id"] for a in out["accounts"]], [str(self.home.id)])
        self.assertEqual(out["accounts"][0]["default"], False)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.home)])

    def test_one_account_that_fails_does_not_hide_the_other(self):
        self.google.failing[token_for(self.home)] = 500
        out = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual({c["account_id"] for c in out["calendars"]}, {str(self.work.id)})
        (failed,) = out["failed_accounts"]
        self.assertEqual(failed["account_id"], str(self.home.id))
        self.assertIn("500", failed["reason"])

    def test_an_account_without_calendar_access_is_reported_not_called(self):
        bare = connect_google(self.user, "sub-bare", "me@bare.example", scopes=["openid"])
        out = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual([f["account_id"] for f in out["failed_accounts"]], [str(bare.id)])
        self.assertNotIn(token_for(bare), {c.token for c in self.google.calls})

    def test_every_account_failing_is_an_error(self):
        self.google.failing[token_for(self.work)] = 500
        self.google.failing[token_for(self.home)] = 500
        with self.assertRaisesRegex(ToolError, "No calendar could be read"):
            LIST_CALENDARS.run({}, self.ctx)

    def test_the_labels_are_the_only_addresses_in_it_and_no_token_is(self):
        text = json.dumps(LIST_CALENDARS.run({}, self.ctx))
        self.assertNotIn("token-", text)
        self.assertNotIn("placeholder", text)
        self.assertNotIn("sub-work", text)

    def test_a_member_with_no_google_account_is_told_so(self):
        lonely = User.objects.create_user(username="l", email="l@x.example", password="pw12345!")
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(lonely.id))
        with self.assertRaisesRegex(ToolError, "not connected"):
            LIST_CALENDARS.run({}, ctx)


# --------------------------------------------------------------------------- #
# Flag off: nothing moves                                                      #
# --------------------------------------------------------------------------- #


class FlagOffTests(CalendarBase):
    flag = False

    def setUp(self):
        super().setUp()
        self.google.add_event(self.work, "primary", "wk1", summary="Standup")
        self.google.add_event(self.home, "primary", "hm1", summary="Dentist")
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        self.google.add_calendar(self.home, "me@home.example", primary=True)

    def test_the_declarations_carry_no_account_and_no_new_tool(self):
        declared = {d.name: d for d in controller._build_tool_declarations()}
        self.assertNotIn("get_calendar_event", declared)
        for name in CALENDAR_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn("account", declared[name].parameters_schema["properties"])
                self.assertEqual(declared[name].description, REGISTRY[name].description)

    def test_the_create_description_has_no_account_hint(self):
        # The hint that sends a member's "personal calendar" through `list_calendars` is the flag's: off, the
        # declared text is the registry's own, byte for byte, and nothing of the sentence is in it.
        declared = {d.name: d.description for d in controller._build_tool_declarations()}
        self.assertEqual(
            declared["create_calendar_event"], REGISTRY["create_calendar_event"].description
        )
        for name in CALENDAR_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(CREATE_ACCOUNT_HINT, declared[name])
                self.assertNotIn("names a specific calendar or account", declared[name])
                self.assertNotIn("names a specific calendar or account", REGISTRY[name].description)

    def test_a_read_that_names_an_account_reads_the_default_as_before(self):
        out = LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.work)])
        self.assertEqual([e["id"] for e in out["events"]], ["wk1"])

    def test_a_write_that_names_an_account_is_refused_and_nothing_is_sent(self):
        # An approval paused with the flag on says "home" on its card. Dropping the argument would put
        # the event on the work calendar when it is approved after the flag went off.
        for tool, args in (
            (CREATE_CALENDAR_EVENT, CREATE_ARGS),
            (UPDATE_CALENDAR_EVENT, {"event_id": "hm1", "summary": "x"}),
            (DELETE_CALENDAR_EVENT, {"event_id": "hm1"}),
        ):
            with self.subTest(tool=tool.name):
                with self.assertRaisesRegex(ToolError, "not available"):
                    tool.run({**args, "account": str(self.home.id)}, self.ctx)
        self.assertEqual(self.google.calls, [])
        self.assertIn((token_for(self.home), "primary", "hm1"), self.google.events)

    def test_results_carry_none_of_the_new_keys(self):
        listed = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual(set(listed), {"calendar_id", "from", "to", "events", "__summary__"})
        self.assertNotIn("cite", listed["events"][0])
        created = CREATE_CALENDAR_EVENT.run(CREATE_ARGS, self.ctx)
        self.assertFalse({"account_id", "cite", "account_label", "other_accounts"} & set(created))
        deleted = DELETE_CALENDAR_EVENT.run({"event_id": "wk1"}, self.ctx)
        self.assertEqual(
            set(deleted), {"event_id", "calendar_id", "deleted", "already_gone", "__summary__"}
        )
        calendars = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual(set(calendars), {"calendars", "__summary__"})
        self.assertEqual(
            set(calendars["calendars"][0]),
            {"id", "summary", "primary", "background_color", "access_role"},
        )
        self.assertEqual(len(calendars["calendars"]), 1)  # the first account only

    def test_a_not_found_says_what_it_always_said(self):
        with self.assertRaises(ToolError) as raised:
            UPDATE_CALENDAR_EVENT.run({"event_id": "nope", "summary": "x"}, self.ctx)
        self.assertEqual(
            str(raised.exception),
            "Event nope not found on calendar `primary`. It may have been deleted upstream.",
        )

    def test_the_system_prompt_is_the_constant(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_CHEATSHEET_FROM_REGISTRY": False}):
            self.assertEqual(agent_system_prompt(), AGENT_SYSTEM_PROMPT)


# --------------------------------------------------------------------------- #
# What the model is shown while the flag is on                                 #
# --------------------------------------------------------------------------- #


class DeclarationTests(CalendarBase):
    def test_every_calendar_tool_declares_account_and_the_new_tool_is_declared_last(self):
        names = [d.name for d in controller._build_tool_declarations()]
        self.assertEqual(names[-2:], ["delete_task_attachment", "get_calendar_event"])
        declared = {d.name: d for d in controller._build_tool_declarations()}
        for name in (*WITH_ACCOUNT, "get_calendar_event"):
            with self.subTest(tool=name):
                prop = declared[name].parameters_schema["properties"]["account"]
                self.assertEqual(prop["type"], "STRING")
                self.assertIn("account_id", prop["description"])
                self.assertNotIn("account", declared[name].parameters_schema.get("required", []))

    def test_the_account_is_never_required(self):
        # Omitted, it is today's behaviour: the default account.
        for name in (*WITH_ACCOUNT, "get_calendar_event"):
            required = {d.name: d for d in controller._build_tool_declarations()}[
                name
            ].parameters_schema.get("required", [])
            self.assertNotIn("account", required, name)

    def test_the_registry_itself_carries_no_account(self):
        for name in WITH_ACCOUNT:
            self.assertNotIn("account", REGISTRY[name].parameters_schema["properties"], name)

    def test_the_new_tool_is_not_in_the_registry(self):
        self.assertNotIn("get_calendar_event", REGISTRY)

    def test_the_descriptions_say_what_the_flag_changes(self):
        declared = {d.name: d.description for d in controller._build_tool_declarations()}
        self.assertIn("EVERY connected Google account", declared["list_calendars"])
        self.assertIn("ONE account", declared["list_calendar_events"])
        self.assertIn("other_accounts", declared["list_calendar_events"])
        self.assertIn("already gone", declared["delete_calendar_event"])
        self.assertNotIn("account", REGISTRY["update_calendar_event"].description)

    def test_create_tells_the_model_to_look_up_a_named_account_first(self):
        # Paid run 2026-11-15: "put it on my personal Google calendar", asked in Japanese, was proposed on the
        # DEFAULT account with no `account` and no `list_calendars` (2 of 3; the English twin looked it up 3 of 3),
        # and the approval card showed the work address. One sentence, on the one tool that proposes before it
        # reads anything, in no one language, that leaves a member who names nothing alone.
        declared = {d.name: d.description for d in controller._build_tool_declarations()}
        self.assertEqual(
            declared["create_calendar_event"],
            REGISTRY["create_calendar_event"].description + CREATE_ACCOUNT_HINT,
        )
        for needle in (
            "names a specific calendar or account",
            "any language",
            "call `list_calendars` first",
            "pass that `account`, or the event goes to the default one",
            "Names none: leave `account` out",
        ):
            self.assertIn(needle, CREATE_ACCOUNT_HINT)
        self.assertIn("list_calendars", declared)  # the tool it names is declared
        self.assertTrue(CREATE_ACCOUNT_HINT.isascii(), "worded for any language, not one")
        # The prefix has about 350 characters of headroom (`test_agent_prefix_budget`): this is one sentence.
        self.assertLessEqual(len(CREATE_ACCOUNT_HINT), 260)

    def test_the_hint_is_on_create_only_the_tools_that_read_first_do_not_carry_it(self):
        declared = {d.name: d.description for d in controller._build_tool_declarations()}
        for name in CALENDAR_TOOLS:
            if name == "create_calendar_event":
                continue
            with self.subTest(tool=name):
                self.assertNotIn("names a specific calendar or account", declared[name])
        # `list_calendars` is still told it may be skipped for a request that names nothing: the hint adds a
        # call only when the member names an account, never for everyone.
        self.assertIn("you can skip this", declared["list_calendars"])

    def test_the_additions_are_filed_once_per_tool(self):
        # The parity flag's rows. MIP-1607's `AGENT_PARITY_FOLLOWUPS` row rewords the one property of the events
        # read and is held by `test_agent_parity_calendar_all_accounts`.
        added = [
            a.tool
            for a in tool_flag_params.ADDITIONS
            if a.tool in CALENDAR_TOOLS and a.flag == PARITY_FLAG
        ]
        self.assertEqual(sorted(added), sorted(CALENDAR_TOOLS))
        sentences = [
            t for t, f, _s in tool_flag_text.ADDITIONS if t in CALENDAR_TOOLS and f == PARITY_FLAG
        ]
        self.assertEqual(len(sentences), len(set(sentences)))

    def test_the_new_tool_reads_and_asks_no_approval(self):
        self.assertFalse(GET_CALENDAR_EVENT.requires_approval)
        self.assertFalse(GET_CALENDAR_EVENT.mcp_only)

    def test_the_system_prompt_lists_the_event_citation_next_to_the_other_shapes(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_CHEATSHEET_FROM_REGISTRY": False}):
            on = agent_system_prompt()
        self.assertIn('"calendar_event:<account_id>:<event_id>"', on)
        todo = on.index('"todo:<local_date>:item:<item_id>"')
        event = on.index('"calendar_event:<account_id>:<event_id>"')
        rules = on.index("Cite ONLY the id shapes shown above")
        self.assertTrue(todo < event < rules)
        # One line; the rest of the prompt is the constant.
        self.assertEqual(
            on.replace(prompts._TODO_SHAPE_END_WITH_EVENT, prompts._TODO_SHAPE_END, 1),
            AGENT_SYSTEM_PROMPT,
        )

    def test_the_registry_generated_prompt_gets_the_same_line(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_CHEATSHEET_FROM_REGISTRY": True}):
            self.assertIn('"calendar_event:<account_id>:<event_id>"', agent_system_prompt())


# --------------------------------------------------------------------------- #
# What the review of this change found                                         #
# --------------------------------------------------------------------------- #


class ReviewFindingsTests(CalendarBase):
    def test_an_event_id_ending_in_a_hyphen_gets_no_token_the_scanner_would_cut(self):
        # `extract_citation_tokens` strips a token's trailing `-` and `:`; an id ending in one would
        # never match its own chip. Google's ids (base32hex) have none.
        self.assertIsNone(event_cite(self.work.id, "abc-"))
        self.assertIsNone(event_cite(self.work.id, "a-b"))
        self.assertIsNotNone(event_cite(self.work.id, "abc_20261201T010000Z"))

    def test_a_calendarlist_item_that_is_not_an_object_does_not_crash_the_listing(self):
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        self.google.calendars[token_for(self.home)] = ["junk", None, {"id": "me@home.example"}]
        out = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual(
            {c["id"] for c in out["calendars"]}, {"me@work.example", "me@home.example"}
        )

    def test_an_account_that_answers_with_an_unreadable_body_is_reported_not_a_crash(self):
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        self.google.unreadable.add(token_for(self.home))
        out = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual(
            [c["account_id"] for c in out["calendars"]], [str(self.work.id)] * len(out["calendars"])
        )
        self.assertEqual([f["account_id"] for f in out["failed_accounts"]], [str(self.home.id)])

    def test_a_no_op_update_still_refuses_an_account_that_is_not_the_members(self):
        for account in (str(self.stranger.id), "not-a-uuid", str(uuid.uuid4())):
            with self.subTest(account=account):
                with self.assertRaises(ToolError):
                    UPDATE_CALENDAR_EVENT.run({"event_id": "e1", "account": account}, self.ctx)
        # and a good one is a plain "no changes", with nothing sent
        out = UPDATE_CALENDAR_EVENT.run({"event_id": "e1", "account": str(self.home.id)}, self.ctx)
        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(self.google.calls, [])

    def test_a_delete_that_finds_nothing_says_where_else_to_look_only_when_there_is_somewhere(self):
        out = DELETE_CALENDAR_EVENT.run({"event_id": "nope"}, self.ctx)
        self.assertTrue(out["already_gone"])
        self.assertIn("another one", out["note"])
        self.assertIn("`account`", out["note"])
        named = DELETE_CALENDAR_EVENT.run(
            {"event_id": "nope", "account": str(self.home.id)}, self.ctx
        )
        self.assertNotIn("note", named)  # the member named the account: nothing to second-guess
        self.home.delete()
        alone = DELETE_CALENDAR_EVENT.run({"event_id": "nope"}, self.ctx)
        self.assertNotIn("note", alone)
        self.google.add_event(self.work, "primary", "real", summary="x")
        deleted = DELETE_CALENDAR_EVENT.run({"event_id": "real"}, self.ctx)
        self.assertNotIn("note", deleted)

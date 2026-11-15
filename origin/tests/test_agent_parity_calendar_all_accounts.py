"""`list_calendar_events` with `account: "all"` (MIP-1607, `AGENT_PARITY_FOLLOWUPS` on top of `AGENT_PARITY_TOOLS`).

The paid run of 2026-11-15 measured a Japanese "what is on my calendar this week" at 4.3 calls instead of 2.3: with
several Google accounts the model read each one separately. With the follow-ups flag on one call reads them all:

  * only the MEMBER'S OWN accounts are read (the list comes from the same `accounts_for` every account lookup uses),
    so there is no id to forge, and naming another member's account is refused as ever;
  * each event carries the `account_id` it came from (and its `cite` token for that account), the accounts that were
    read are listed with their label, the events are in the order they start across accounts and time zones;
  * an account that cannot be read is reported under `failed_accounts` (as `list_calendars` does) and does not hide the
    others; every account failing is an error;
  * each account is read with the same query a single read sends (so `max_results` is per account, as today), the
    answer holds at most 100 events (`events_not_shown` says how many were left out) and at most 10 accounts are read.

With the flag off `"all"` is the unknown account it always was, byte for byte; the fingerprint golden holds the rest.
Google is a fake (`calendar_fakes.FakeGoogle`): each call is recorded with the token of the account it was made with.
"""

from __future__ import annotations

import copy
import uuid
from unittest import mock

from django.conf import settings

from origin.models.common.api_key_models import SCOPE_READ
from origin.search_engine.agent import controller, tool_flag_params, tool_flag_text
from origin.search_engine.agent.calendar import ACCOUNT_REFUSAL
from origin.search_engine.agent.parity import FOLLOWUPS_FLAG, PARITY_FLAG
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools import list_calendar_events as events_tool
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.services.oauth.accounts import accounts_for
from origin.views.public.mcp import tools as mcp

from . import test_agent_parity_calendar as calendar_tests
from . import test_mcp_parity_calendar as mcp_calendar_tests
from .calendar_fakes import connect_google, token_for

ALL = {"account": "all"}
BOTH_ON = {PARITY_FLAG: True, FOLLOWUPS_FLAG: True}
EXPECT_KEYS = {
    "calendar_id",
    "from",
    "to",
    "account",
    "accounts",
    "events",
    "__summary__",
}


class FollowupsBase(calendar_tests.CalendarBase):
    """`self.user`: `work` (the default, Standup at 10:00 +09:00) and `home` (Dentist at 09:30 +00:00, which is
    18:30 in Tokyo); `self.stranger` belongs to someone else and has an event on `primary` too."""

    followups = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FOLLOWUPS_FLAG: self.followups})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.google.add_event(self.work, "primary", "wk1", summary="Standup")
        self.google.add_event(
            self.home,
            "primary",
            "hm1",
            summary="Dentist",
            start={"dateTime": "2026-12-01T09:30:00+00:00"},
            end={"dateTime": "2026-12-01T10:30:00+00:00"},
        )
        self.google.add_event(self.stranger, "primary", "st1", summary="Stranger secret")
        self.google.add_calendar(self.work, "me@work.example", primary=True)
        self.google.add_calendar(self.home, "me@home.example", primary=True)

    def read(self, args=None):
        return LIST_CALENDAR_EVENTS.run({**ALL, **(args or {})}, self.ctx)


class AllAccountsTests(FollowupsBase):
    def test_one_call_reads_every_account_and_each_event_says_which_account_it_is_on(self):
        out = self.read()
        self.assertEqual(
            [(e["id"], e["account_id"]) for e in out["events"]],
            # 09:30 UTC is 18:30 in Tokyo: after the 10:00 +09:00 Standup, whatever the order of the accounts
            [("wk1", str(self.work.id)), ("hm1", str(self.home.id))],
        )
        self.assertEqual(
            [token_for(self.work), token_for(self.home)], [c.token for c in self.google.calls]
        )
        self.assertEqual(out["account"], "all")
        self.assertEqual(
            out["accounts"],
            [
                {"account_id": str(self.work.id), "label": "me@work.example", "events": 1},
                {"account_id": str(self.home.id), "label": "me@home.example", "events": 1},
            ],
        )
        self.assertEqual(set(out), EXPECT_KEYS)
        self.assertNotIn("other_accounts", out)

    def test_it_is_the_union_of_the_single_account_reads(self):
        # The twin: the events of "all" are exactly what the two single reads return, plus `account_id`.
        self.google.add_event(
            self.work,
            "primary",
            "wk0",
            summary="Early",
            start={"dateTime": "2026-12-01T01:00:00+09:00"},
            end={"dateTime": "2026-12-01T02:00:00+09:00"},
        )
        single = []
        for account in (self.work, self.home):
            read = LIST_CALENDAR_EVENTS.run({"account": str(account.id)}, self.ctx)
            single += [{**e, "account_id": str(account.id)} for e in read["events"]]
        combined = self.read()["events"]
        self.assertEqual(
            sorted(combined, key=lambda e: e["id"]), sorted(single, key=lambda e: e["id"])
        )
        self.assertEqual([e["id"] for e in combined], ["wk0", "wk1", "hm1"])

    def test_each_account_is_asked_the_query_a_single_read_sends(self):
        args = {
            "from": "2026-12-01T00:00:00Z",
            "to": "2026-12-08T00:00:00Z",
            "query": " stand ",
            "max_results": 7,
        }
        self.google.calls.clear()
        LIST_CALENDAR_EVENTS.run({"account": str(self.work.id), **args}, self.ctx)
        single = self.google.calls[0]
        self.google.calls.clear()
        out = self.read(args)
        self.assertEqual(len(self.google.calls), 2)
        for call in self.google.calls:
            self.assertEqual(
                (call.method, call.path, call.params), (single.method, single.path, single.params)
            )
        self.assertEqual((out["from"], out["to"]), (args["from"], args["to"]))
        self.assertEqual(single.params["maxResults"], "7")

    def test_a_calendar_id_goes_to_every_account_and_a_miss_is_a_failed_account_not_a_crash(self):
        self.google.add_event(self.work, "me@work.example", "w9", summary="Own calendar")
        out = self.read({"calendar_id": "me@work.example"})
        self.assertEqual([e["id"] for e in out["events"]], ["w9"])
        self.assertEqual(out["calendar_id"], "me@work.example")
        # The fake answers an empty list for a calendar an account does not have: both were asked.
        self.assertEqual(
            {c.token for c in self.google.calls}, {token_for(self.work), token_for(self.home)}
        )

    def test_events_carry_the_token_to_cite_them_by_for_their_own_account(self):
        events = {e["id"]: e for e in self.read()["events"]}
        self.assertEqual(events["wk1"]["cite"], f"calendar_event:{self.work.id}:wk1")
        self.assertEqual(events["hm1"]["cite"], f"calendar_event:{self.home.id}:hm1")

    def test_mcp_is_given_no_citation_grammar(self):
        ctx = ToolContext(team_id=self.ctx.team_id, user_id=self.ctx.user_id, surface="mcp")
        out = LIST_CALENDAR_EVENTS.run(ALL, ctx)
        self.assertEqual(len(out["events"]), 2)
        for event in out["events"]:
            self.assertNotIn("cite", event)
            self.assertIn("account_id", event)

    def test_the_spelling_of_all_does_not_matter(self):
        for spelling in ("ALL", " All ", "all"):
            with self.subTest(spelling=spelling):
                out = LIST_CALENDAR_EVENTS.run({"account": spelling}, self.ctx)
                self.assertEqual(out["account"], "all")

    def test_a_member_with_one_account_gets_that_account_back(self):
        lonely = calendar_tests.User.objects.create_user(
            username="l", email="l@x.example", password="pw12345!"
        )
        solo = connect_google(lonely, "sub-solo", "l@solo.example", login=True)
        self.google.add_event(solo, "primary", "s1", summary="Solo")
        out = LIST_CALENDAR_EVENTS.run(
            ALL, ToolContext(team_id=self.ctx.team_id, user_id=str(lonely.id))
        )
        self.assertEqual(
            [(e["id"], e["account_id"]) for e in out["events"]], [("s1", str(solo.id))]
        )

    def test_a_member_with_no_google_account_is_told_so(self):
        nobody = calendar_tests.User.objects.create_user(
            username="n", email="n@x.example", password="pw12345!"
        )
        with self.assertRaisesRegex(ToolError, "not connected"):
            LIST_CALENDAR_EVENTS.run(
                ALL, ToolContext(team_id=self.ctx.team_id, user_id=str(nobody.id))
            )
        self.assertEqual(self.google.calls, [])

    def test_a_bad_max_results_is_refused_before_google_is_called(self):
        with self.assertRaisesRegex(ToolError, "max_results"):
            self.read({"max_results": "many"})
        self.assertEqual(self.google.calls, [])


class OwnershipTests(FollowupsBase):
    def test_another_members_events_are_never_read_or_returned(self):
        out = self.read()
        self.assertNotIn("st1", {e["id"] for e in out["events"]})
        self.assertNotIn(token_for(self.stranger), {c.token for c in self.google.calls})
        self.assertNotIn(str(self.stranger.id), str(out))
        self.assertNotIn("you@elsewhere.example", str(out))

    def test_all_does_not_open_the_door_to_a_named_foreign_account(self):
        for value in (str(self.stranger.id), str(uuid.uuid4()), "me@work.example", "everything"):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as raised:
                    LIST_CALENDAR_EVENTS.run({"account": value}, self.ctx)
                self.assertEqual(str(raised.exception), ACCOUNT_REFUSAL)
        with self.assertRaises(ToolForbidden):
            LIST_CALENDAR_EVENTS.run({"account": str(self.stranger.id)}, self.ctx)
        self.assertEqual(self.google.calls, [])

    def test_the_other_member_reads_only_their_own_accounts_with_all(self):
        out = LIST_CALENDAR_EVENTS.run(
            ALL, ToolContext(team_id=str(self.team.team_id), user_id=str(self.other.id))
        )
        self.assertEqual([e["id"] for e in out["events"]], ["st1"])
        self.assertEqual({c.token for c in self.google.calls}, {token_for(self.stranger)})

    def test_a_github_account_of_the_member_is_not_read(self):
        type(self.work).objects.create(
            user=self.user, provider="github", provider_user_id="gh-1", access_token_encrypted="x"
        )
        self.read()
        self.assertEqual(len(self.google.calls), 2)


class FailingAccountTests(FollowupsBase):
    def test_one_account_that_fails_does_not_hide_the_other(self):
        self.google.failing[token_for(self.home)] = 500
        out = self.read()
        self.assertEqual([e["id"] for e in out["events"]], ["wk1"])
        self.assertEqual(
            out["failed_accounts"],
            [{"account_id": str(self.home.id), "reason": "Google answered HTTP 500"}],
        )
        self.assertEqual([a["account_id"] for a in out["accounts"]], [str(self.work.id)])

    def test_an_account_without_calendar_access_is_reported_and_not_called(self):
        bare = connect_google(self.user, "sub-bare", "me@bare.example", scopes=["openid"])
        out = self.read()
        self.assertIn(
            {"account_id": str(bare.id), "reason": "Calendar access not granted"},
            out["failed_accounts"],
        )
        self.assertNotIn(token_for(bare), {c.token for c in self.google.calls})
        self.assertEqual(len(out["events"]), 2)

    def test_an_account_whose_sign_in_expired_is_reported(self):
        from origin.services.oauth.tokens import ReauthRequired  # noqa: PLC0415

        real = token_for

        def token(account):
            if account.id == self.home.id:
                raise ReauthRequired(account)
            return real(account)

        with mock.patch(
            "origin.search_engine.agent.calendar.get_valid_access_token", side_effect=token
        ):
            out = self.read()
        self.assertEqual(
            out["failed_accounts"],
            [{"account_id": str(self.home.id), "reason": "Google access expired"}],
        )
        self.assertEqual([e["id"] for e in out["events"]], ["wk1"])

    def test_an_unreadable_reply_and_an_unreachable_google_are_reported(self):
        import requests  # noqa: PLC0415

        self.google.unreadable.add(token_for(self.home))
        out = self.read()
        self.assertEqual(out["failed_accounts"][0]["reason"], "Google sent an unreadable reply")
        self.google.unreadable.clear()
        real = self.google.request

        def flaky(method, url, **kwargs):
            if kwargs["headers"]["Authorization"].endswith(token_for(self.work)):
                raise requests.ConnectionError("me@work.example is down")
            return real(method, url, **kwargs)

        with mock.patch("requests.request", side_effect=flaky):
            out = self.read()
        self.assertEqual(out["failed_accounts"][0]["reason"], "Google could not be reached")
        self.assertNotIn("me@work.example", str(out))

    def test_every_account_failing_is_an_error_with_the_first_ones_words(self):
        for account in (self.work, self.home):
            self.google.failing[token_for(account)] = 503
        with self.assertRaisesRegex(
            ToolError, r"No events could be read: Google answered HTTP 503\."
        ):
            self.read()

    def test_a_failure_puts_no_address_and_no_body_in_the_result_or_a_log(self):
        self.google.failing[token_for(self.home)] = 500
        with self.assertNoLogs(level="WARNING"):
            out = self.read({"calendar_id": "me@home.example"})
        self.assertNotIn("me@home.example", str(out["failed_accounts"]))
        self.assertNotIn("Not found", str(out))


class CapTests(FollowupsBase):
    def test_the_answer_holds_at_most_100_events_and_says_how_many_it_left_out(self):
        for i in range(60):
            for account in (self.work, self.home):
                self.google.add_event(
                    account,
                    "primary",
                    f"{account.provider_user_id}-{i:02d}",
                    start={"dateTime": f"2026-12-02T{i % 24:02d}:00:00+09:00"},
                    end={"dateTime": f"2026-12-02T{i % 24:02d}:30:00+09:00"},
                )
        out = self.read()
        self.assertEqual(len(out["events"]), 100)
        self.assertEqual(out["events_not_shown"], 22)
        starts = [events_tool._start_key(e) for e in out["events"]]
        self.assertEqual(starts, sorted(starts))  # the cut is of the LATEST events
        # `accounts` still says how many each account returned.
        self.assertEqual([a["events"] for a in out["accounts"]], [61, 61])

    def test_under_the_cap_nothing_is_said_to_be_left_out(self):
        self.assertNotIn("events_not_shown", self.read())

    def test_at_most_ten_accounts_are_read_and_the_rest_are_said_not_to_be(self):
        for i in range(9):  # work and home are two: eleven in all
            connect_google(self.user, f"sub-x{i}", f"x{i}@example.com")
        last = list(accounts_for(self.user, "google"))[
            -1
        ]  # the one past the tenth, in the default order
        out = self.read()
        self.assertEqual(len(self.google.calls), 10)
        self.assertEqual(len(out["accounts"]), 10)
        self.assertEqual(
            out["failed_accounts"],
            [{"account_id": str(last.id), "reason": "Not read: too many accounts"}],
        )

    def test_max_results_stays_per_account_and_is_still_clamped(self):
        self.read({"max_results": 5000})
        self.assertEqual({c.params["maxResults"] for c in self.google.calls}, {"100"})
        self.google.calls.clear()
        self.read()
        self.assertEqual({c.params["maxResults"] for c in self.google.calls}, {"50"})


class OrderingTests(FollowupsBase):
    def test_all_day_events_zones_and_unreadable_starts_sort_by_the_instant(self):
        key = events_tool._start_key
        self.assertLess(key({"start": "2026-12-01"}), key({"start": "2026-12-01T00:00:01+00:00"}))
        self.assertLess(
            key({"start": "2026-12-01T09:00:00+09:00"}), key({"start": "2026-12-01T01:00:00+00:00"})
        )
        self.assertEqual(key({"start": "2026-12-01T00:00:00"}), key({"start": "2026-12-01"}))
        self.assertLess(key({"start": "2026-12-01T00:00:00Z"}), key({"start": "garbage"}))
        self.assertEqual(key({"start": None}), key({}))


class ReadOfOneAccountTests(FollowupsBase):
    def test_a_default_read_of_a_member_with_several_accounts_says_how_to_read_them_all(self):
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual(out["other_accounts"], 1)
        self.assertEqual(out["hint"], events_tool.OTHER_ACCOUNTS_HINT)
        self.assertEqual(len(self.google.calls), 1)

    def test_a_named_read_and_a_one_account_member_get_no_hint(self):
        self.assertNotIn("hint", LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx))
        lonely = calendar_tests.User.objects.create_user(
            username="l", email="l@x.example", password="pw12345!"
        )
        connect_google(lonely, "sub-solo", "l@solo.example", login=True)
        out = LIST_CALENDAR_EVENTS.run(
            {}, ToolContext(team_id=self.ctx.team_id, user_id=str(lonely.id))
        )
        self.assertNotIn("hint", out)
        self.assertNotIn("other_accounts", out)


class FlagOffTests(FollowupsBase):
    """`AGENT_PARITY_FOLLOWUPS` off with the parity flag ON (production today): `account: "all"` is the unknown
    account it always was, and a result carries what it carried before."""

    followups = False

    def test_all_is_refused_as_an_unknown_account_and_nothing_is_sent(self):
        for spelling in ("all", "ALL", " all "):
            with self.subTest(spelling=spelling):
                with self.assertRaises(ToolError) as raised:
                    LIST_CALENDAR_EVENTS.run({"account": spelling}, self.ctx)
                self.assertEqual(str(raised.exception), ACCOUNT_REFUSAL)
                self.assertNotIsInstance(raised.exception, ToolForbidden)
        self.assertEqual(self.google.calls, [])

    def test_the_refusal_is_the_one_a_made_up_value_gets(self):
        with self.assertRaises(ToolError) as nothing:
            LIST_CALENDAR_EVENTS.run({"account": "everything"}, self.ctx)
        with self.assertRaises(ToolError) as everything:
            LIST_CALENDAR_EVENTS.run(ALL, self.ctx)
        self.assertEqual(str(nothing.exception), str(everything.exception))

    def test_a_default_read_has_the_keys_it_had_and_no_hint(self):
        out = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual(
            set(out),
            {
                "calendar_id",
                "from",
                "to",
                "events",
                "__summary__",
                "account_id",
                "account_label",
                "other_accounts",
            },
        )

    def test_the_declaration_is_what_the_parity_flag_alone_declares(self):
        declared = {d.name: d for d in controller._build_tool_declarations()}
        events = declared["list_calendar_events"]
        self.assertEqual(
            events.description,
            REGISTRY["list_calendar_events"].description + events_tool.DESCRIPTION_ADDITION,
        )
        self.assertEqual(
            events.parameters_schema["properties"]["account"]["description"],
            "account_id from list_calendars. Omit for the default account.",
        )
        self.assertNotIn(
            "all", events.parameters_schema["properties"]["account"]["description"].lower().split()
        )


class ParityOffTests(FollowupsBase):
    """The follow-ups flag cannot publish `account` where the parity flag does not: the parameter does not exist."""

    flag = False

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FOLLOWUPS_FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_is_ignored_like_any_argument_the_parity_off_read_does_not_declare(self):
        out = LIST_CALENDAR_EVENTS.run(ALL, self.ctx)
        self.assertEqual([c.token for c in self.google.calls], [token_for(self.work)])
        self.assertNotIn("accounts", out)
        self.assertNotIn("hint", out)

    def test_no_parameter_is_declared(self):
        declared = {d.name: d for d in controller._build_tool_declarations()}
        self.assertNotIn(
            "account", declared["list_calendar_events"].parameters_schema["properties"]
        )


class DeclarationTests(FollowupsBase):
    def declared(self):
        return {d.name: d for d in controller._build_tool_declarations()}

    def test_the_account_of_the_events_read_names_all_and_no_other_tool_does(self):
        declared = self.declared()
        self.assertEqual(
            declared["list_calendar_events"].parameters_schema["properties"]["account"][
                "description"
            ],
            events_tool.FOLLOWUPS_ACCOUNT_DESCRIPTION,
        )
        for name in (
            "list_calendars",
            "create_calendar_event",
            "update_calendar_event",
            "delete_calendar_event",
            "get_calendar_event",
        ):
            with self.subTest(tool=name):
                self.assertNotIn(
                    "all",
                    declared[name]
                    .parameters_schema["properties"]["account"]["description"]
                    .split(),
                )

    def test_the_description_says_when_to_use_it_after_the_parity_sentence(self):
        text = self.declared()["list_calendar_events"].description
        self.assertEqual(
            text,
            REGISTRY["list_calendar_events"].description
            + events_tool.DESCRIPTION_ADDITION
            + events_tool.FOLLOWUPS_DESCRIPTION_ADDITION,
        )
        sentence = events_tool.FOLLOWUPS_DESCRIPTION_ADDITION
        # It names where the member's number of accounts comes from, so the model never calls a tool for it.
        for needle in ("several accounts", "list_calendars", "other_accounts", '"all"', "one call"):
            self.assertIn(needle, sentence)
        self.assertNotIn(
            "first", sentence.lower()
        )  # it must not tell the model to look anything up first
        self.assertTrue(sentence.isascii(), "worded for any language")

    def test_the_static_text_this_adds_is_small(self):
        # Both the sentence and the reworded property count against the prefix budget on every call.
        added = len(events_tool.FOLLOWUPS_DESCRIPTION_ADDITION) + (
            len(events_tool.FOLLOWUPS_ACCOUNT_DESCRIPTION)
            - len("account_id from list_calendars. Omit for the default account.")
        )
        # 157 when the sentence was first measured; the first paid run added "omit `account` unless ..." for the member
        # with one account (+~60): the budget numbers in `test_agent_prefix_budget` hold the total.
        self.assertLessEqual(added, 230)

    def test_the_sentence_tells_the_model_to_omit_account_unless_it_is_named_or_all_are_wanted(
        self,
    ):
        # First paid run: a member with ONE account was sent an `account` in most runs.
        sentence = events_tool.FOLLOWUPS_DESCRIPTION_ADDITION
        self.assertIn(
            "Omit `account` unless the member names an account or asks for all of them", sentence
        )
        with mock.patch.dict(settings.SEARCH_ENGINE, {FOLLOWUPS_FLAG: False}):
            self.assertNotIn(
                "Omit `account` unless", self.declared()["list_calendar_events"].description
            )

    def test_the_registry_and_its_schema_are_not_edited(self):
        before = copy.deepcopy(REGISTRY["list_calendar_events"].parameters_schema)
        self.declared()
        self.assertEqual(REGISTRY["list_calendar_events"].parameters_schema, before)
        self.assertNotIn("account", before["properties"])

    def test_the_followups_rows_are_filed_once_and_are_the_only_ones_of_their_flag(self):
        rows = [a for a in tool_flag_params.ADDITIONS if a.flag == FOLLOWUPS_FLAG]
        self.assertEqual([a.tool for a in rows], ["list_calendar_events"])
        self.assertEqual(rows[0].properties, {})
        # (Other follow-ups file their own sentence on their own tool, e.g. MIP-1589's `list_project_tags`.)
        sentences = [
            t
            for t, f, _s in tool_flag_text.ADDITIONS
            if f == FOLLOWUPS_FLAG and t.endswith("events")
        ]
        self.assertEqual(sentences, ["list_calendar_events"])

    def test_the_account_is_still_never_required(self):
        schema = self.declared()["list_calendar_events"].parameters_schema
        self.assertNotIn("account", schema.get("required", []))


class McpTests(mcp_calendar_tests.CalendarMcpBase):
    flag = True
    followups = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FOLLOWUPS_FLAG: self.followups})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_schema_and_description_say_all_while_the_flag_is_on(self):
        self._key(scope=SCOPE_READ)
        listed = self.listed()["list_calendar_events"]
        self.assertEqual(
            listed["inputSchema"]["properties"]["account"]["description"],
            events_tool.FOLLOWUPS_ACCOUNT_DESCRIPTION,
        )
        self.assertIn(
            events_tool.FOLLOWUPS_DESCRIPTION_ADDITION.replace("list_calendars", "list_calendars"),
            listed["description"],
        )

    def test_a_key_reads_every_account_of_its_owner_in_one_call(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(self._call("list_calendar_events", ALL))
        self.assertFalse(is_error, text)
        self.assertIn("Standup", text)
        self.assertIn("Dentist", text)
        self.assertIn(str(self.home.id), text)
        self.assertNotIn('"cite"', text)
        self.assertNotIn("you@elsewhere.example", text)
        self.assertEqual(
            {c.token for c in self.google.calls}, {token_for(self.work), token_for(self.home)}
        )

    def test_another_members_account_is_still_refused(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(
            self._call("list_calendar_events", {"account": str(self.stranger.id)})
        )
        self.assertTrue(is_error)
        self.assertIn(ACCOUNT_REFUSAL, text)
        self.assertEqual(self.google.calls, [])


class McpFlagOffTests(McpTests):
    followups = False

    def test_the_schema_and_description_say_all_while_the_flag_is_on(self):
        self._key(scope=SCOPE_READ)
        listed = self.listed()["list_calendar_events"]
        self.assertNotIn(
            "all", listed["inputSchema"]["properties"]["account"]["description"].split()
        )
        self.assertNotIn(events_tool.FOLLOWUPS_DESCRIPTION_ADDITION, listed["description"])
        self.assertEqual(
            listed["description"],
            mcp.description_for(next(t for t in mcp.CATALOG if t.name == "list_calendar_events")),
        )

    def test_a_key_reads_every_account_of_its_owner_in_one_call(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(self._call("list_calendar_events", ALL))
        self.assertTrue(is_error)
        self.assertIn(ACCOUNT_REFUSAL, text)
        self.assertEqual(self.google.calls, [])

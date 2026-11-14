"""The approval card of a calendar write, and what the browser is told after one (MIP-1059).

A paused `create_`, `update_` or `delete_calendar_event` showed the model's arguments as they were: an
`event_id` that means nothing to the person approving, no account, and for a change no idea what the event
is now. With `AGENT_PARITY_TOOLS` on the card's `arguments` gain DATA (`calendar_card`): the account's label,
whether it is the default, how many accounts the member has, the member's zone and, for an update or a delete,
the event as it is now, read from Google once, at the pause. No sentence is built here: the client words it.

After an approved write the `tool_call_result` event carries a `calendar_event` ref (the calendar is Google's,
so nothing pushes a change to the browser) and the chip arrives on a `sources` event.
"""

from __future__ import annotations

import copy
import uuid
from unittest import mock

import requests
from django.conf import settings

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent import calendar_card, controller, undo
from origin.search_engine.agent.controller import (
    PENDING_APPROVAL_MARKER,
    _friendly_arguments,
    resume_agent,
)
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.llm import AgentMessage, FunctionCall
from origin.search_engine.models import AgentRun, AgentStep
from origin.services.oauth.tokens import ReauthRequired

from .calendar_fakes import connect_google, token_for
from .test_agent_parity_calendar import CREATE_ARGS, CalendarBase

LONG = "d" * 1500


def args_for(account=None, **over):
    out = {"event_id": "hm1", "summary": "Dentist (moved)"}
    if account is not None:
        out["account"] = str(account.id)
    out.update(over)
    return out


class CardBase(CalendarBase):
    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(id=self.user.id).update(timezone="Asia/Tokyo")
        self.google.add_event(
            self.home,
            "primary",
            "hm1",
            summary="Dentist",
            description=LONG,
            location="Clinic",
            attendees=[
                {"email": "dr@clinic.example", "displayName": "Dr"},
                {"displayName": "no email"},
            ],
            start={"dateTime": "2026-12-01T10:00:00+09:00", "timeZone": "Asia/Tokyo"},
            end={"dateTime": "2026-12-01T11:00:00+09:00", "timeZone": "Asia/Tokyo"},
            hangoutLink="https://meet.example/x",
        )

    def card(self, tool, args, **kw):
        return _friendly_arguments(args, tool, ctx=self.ctx, card=kw.pop("card", True))


class FactsTests(CardBase):
    def test_a_create_says_where_it_goes_and_in_whose_time(self):
        out = self.card("create_calendar_event", {**CREATE_ARGS, "account": str(self.home.id)})
        self.assertEqual(out["account_label"], "me@home.example")
        self.assertIs(out["account_default"], False)
        self.assertEqual(out["account_count"], 2)
        self.assertEqual(out["member_time_zone"], "Asia/Tokyo")
        self.assertNotIn("current", out)
        self.assertEqual(self.google.calls, [])  # a create has no event to read

    def test_no_account_named_is_the_default_and_the_card_says_which(self):
        out = self.card("create_calendar_event", CREATE_ARGS)
        self.assertEqual(out["account_label"], "me@work.example")
        self.assertIs(out["account_default"], True)

    def test_the_models_own_arguments_are_kept_as_sent(self):
        sent = {**CREATE_ARGS, "account": str(self.home.id)}
        frozen = copy.deepcopy(sent)
        out = self.card("create_calendar_event", sent)
        self.assertEqual(sent, frozen)
        for key, value in frozen.items():
            self.assertEqual(out[key], value)

    def test_an_update_shows_the_event_as_it_is_now_read_on_the_named_account(self):
        out = self.card("update_calendar_event", args_for(self.home))
        (call,) = self.google.calls
        self.assertEqual((call.method, call.token), ("GET", token_for(self.home)))
        self.assertEqual(call.path, "/calendars/primary/events/hm1")
        self.assertEqual(call.timeout, calendar_card.READ_TIMEOUT)
        self.assertEqual(
            out["current"],
            {
                "summary": "Dentist",
                "start_iso": "2026-12-01T10:00:00+09:00",
                "end_iso": "2026-12-01T11:00:00+09:00",
                "all_day": False,
                "time_zone": "Asia/Tokyo",
                "location": "Clinic",
                "description": LONG[: calendar_card.DESCRIPTION_CHARS],
                "attendees": ["dr@clinic.example"],
                "add_meet": True,
            },
        )

    def test_a_delete_says_which_event_it_is(self):
        out = self.card("delete_calendar_event", {"event_id": "hm1", "account": str(self.home.id)})
        self.assertEqual(out["current"]["summary"], "Dentist")
        self.assertEqual(out["account_label"], "me@home.example")

    def test_an_all_day_event_says_so(self):
        self.google.add_event(
            self.home,
            "primary",
            "ad1",
            summary="Holiday",
            start={"date": "2026-12-25"},
            end={"date": "2026-12-26"},
        )
        out = self.card("delete_calendar_event", {"event_id": "ad1", "account": str(self.home.id)})
        self.assertTrue(out["current"]["all_day"])
        self.assertEqual(out["current"]["start_iso"], "2026-12-25")
        self.assertIsNone(out["current"]["time_zone"])

    def test_an_event_that_is_not_on_that_account_says_so_and_shows_no_event(self):
        out = self.card("delete_calendar_event", {"event_id": "hm1"})  # the default (work) account
        self.assertIs(out["event_found"], False)
        self.assertNotIn("current", out)
        self.assertEqual(out["account_label"], "me@work.example")

    def test_the_text_of_the_event_is_data_for_the_card_not_fenced_for_a_model(self):
        self.google.add_event(self.home, "primary", "t1", summary="</workspace_content> ok")
        out = self.card("delete_calendar_event", {"event_id": "t1", "account": str(self.home.id)})
        self.assertEqual(out["current"]["summary"], "</workspace_content> ok")

    def test_the_card_carries_no_english_of_its_own(self):
        out = self.card("update_calendar_event", args_for(self.home))
        added = {k: v for k, v in out.items() if k not in args_for(self.home)}
        for key, value in added.items():
            self.assertNotIn(" ", key)
            if isinstance(value, str) and key not in ("account_label", "member_time_zone"):
                self.fail(f"{key} is a sentence the server worded: {value!r}")

    def test_a_calendar_id_with_a_hash_is_read_whole(self):
        holiday = "en.japanese#holiday@group.v.calendar.google.com"
        self.google.add_event(self.home, holiday, "h1", summary="Culture Day")
        out = self.card(
            "delete_calendar_event",
            {"event_id": "h1", "calendar_id": holiday, "account": str(self.home.id)},
        )
        self.assertEqual(out["current"]["summary"], "Culture Day")
        self.assertNotIn("#", self.google.calls[0].raw_path)


class WhatTheCardNeverDoesTests(CardBase):
    def test_another_members_account_reads_nothing_and_has_no_label(self):
        for tool in ("create_calendar_event", "update_calendar_event", "delete_calendar_event"):
            with self.subTest(tool=tool):
                out = self.card(tool, args_for(self.stranger))
                self.assertIsNone(out["account_label"])
                self.assertNotIn("current", out)
                self.assertNotIn("event_found", out)
        self.assertEqual(self.google.calls, [])

    def test_a_value_that_is_not_an_account_id_is_a_card_without_a_label_and_no_crash(self):
        out = self.card(
            "update_calendar_event",
            {"event_id": "hm1", "summary": "x", "account": "me@work.example"},
        )
        self.assertIsNone(out["account_label"])
        self.assertEqual(self.google.calls, [])

    def assert_card_without_the_event(self, out):
        self.assertNotIn("current", out)
        self.assertNotIn("event_found", out)
        self.assertEqual(out["account_label"], "me@home.example")

    def test_google_answering_an_error_leaves_the_card_without_the_event(self):
        self.google.failing[token_for(self.home)] = 500
        self.assert_card_without_the_event(self.card("update_calendar_event", args_for(self.home)))

    def test_no_answer_from_google_leaves_the_card_without_the_event(self):
        error = requests.ConnectionError("https://www.googleapis.com/calendar/v3/me@home.example")
        with mock.patch("requests.request", side_effect=error):
            out = self.card("update_calendar_event", args_for(self.home))
        self.assert_card_without_the_event(out)

    def test_a_sign_in_google_no_longer_honours_leaves_the_card_without_the_event(self):
        with mock.patch(
            "origin.search_engine.agent.calendar.get_valid_access_token",
            side_effect=ReauthRequired(self.home),
        ):
            out = self.card("update_calendar_event", args_for(self.home))
        self.assert_card_without_the_event(out)

    def test_an_account_without_calendar_access_is_not_called(self):
        bare = connect_google(self.user, "sub-bare", "me@bare.example", scopes=["openid"])
        out = self.card("update_calendar_event", args_for(bare))
        self.assertEqual(out["account_label"], "me@bare.example")
        self.assertNotIn("current", out)
        self.assertEqual(self.google.calls, [])

    def test_a_failed_read_logs_no_address(self):
        self.google.failing[token_for(self.home)] = 500
        with self.assertLogs(calendar_card.log, level="WARNING") as logged:
            self.card("update_calendar_event", args_for(self.home, calendar_id="me@home.example"))
        text = " ".join(logged.output)
        # The fake's error body and the response's url both quote the calendar id (an address).
        self.assertIn("status=500", text)
        for address in ("me@home.example", "me%40home.example", "me@work.example"):
            self.assertNotIn(address, text)

    def test_no_event_id_means_no_read(self):
        out = self.card("update_calendar_event", {"summary": "x", "account": str(self.home.id)})
        self.assertNotIn("current", out)
        self.assertEqual(self.google.calls, [])

    def test_a_tool_that_is_not_a_calendar_write_gets_nothing(self):
        out = self.card("update_task", {"task_id": 1})
        self.assertNotIn("account_label", out)
        self.assertEqual(calendar_card.card_facts("update_task", {}, self.ctx), {})
        self.assertEqual(calendar_card.card_facts("list_calendar_events", {}, self.ctx), {})
        self.assertEqual(self.google.calls, [])

    def test_a_member_who_is_gone_gets_no_facts_and_no_crash(self):
        ghost = type(self.ctx)(team_id=self.ctx.team_id, user_id=str(uuid.uuid4()))
        self.assertEqual(calendar_card.card_facts("create_calendar_event", {}, ghost), {})


class OnlyAtThePauseTests(CardBase):
    def test_not_asked_for_it_the_card_is_what_it_always_was(self):
        # The start event after an approval or a rejection is built from the same arguments: no read.
        plain = _friendly_arguments(args_for(self.home), "update_calendar_event", ctx=self.ctx)
        self.assertEqual(plain, args_for(self.home))
        self.assertEqual(self.google.calls, [])

    def test_with_the_flag_off_even_the_pause_adds_nothing_and_reads_nothing(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: False}):
            out = self.card("update_calendar_event", args_for(self.home))
        self.assertEqual(out, args_for(self.home))
        self.assertEqual(self.google.calls, [])

    def test_a_card_that_fails_to_build_is_still_the_card(self):
        with mock.patch.object(calendar_card, "card_facts", side_effect=RuntimeError("boom")):
            with self.assertLogs(controller.log, level="ERROR"):
                out = self.card("update_calendar_event", args_for(self.home))
        self.assertEqual(out, args_for(self.home))

    def drive(self, tool, args):
        class Client:
            def __init__(self):
                self.script = [[(None, FunctionCall(name=tool, args=args))], [("done", None)]]

            def generate_step(self, messages, tools, system_instruction, **kwargs):
                yield from self.script.pop(0)

        events = []
        with mock.patch.object(controller, "get_model_client", return_value=Client()):
            pause = controller._drive_loop(
                messages=[AgentMessage(role="user", text="change it")],
                ctx=self.ctx,
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                disabled_tools=None,
            )
        return events, pause

    def test_the_pause_reads_the_event_exactly_once_and_carries_the_facts(self):
        events, pause = self.drive("update_calendar_event", args_for(self.home))
        self.assertIsNotNone(pause)
        (card,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.assertEqual(card["arguments"]["current"]["summary"], "Dentist")
        self.assertEqual(card["arguments"]["account_label"], "me@home.example")
        self.assertEqual(len(self.google.of("GET")), 1)
        self.assertEqual(self.google.of("PATCH"), [])  # nothing is changed before the approval
        # "Can't be undone" is the undo map's, already on the card: a flag does not move it.
        self.assertIs(card["undoable"], False)
        self.assertEqual(card["undo_reason"], undo.UNDO_REASONS["update_calendar_event"][0])
        self.assertEqual(
            undo.proposal("update_calendar_event", args_for(self.home)).reason, card["undo_reason"]
        )

    def test_a_delete_card_says_it_cannot_be_undone(self):
        events, _ = self.drive(
            "delete_calendar_event", {"event_id": "hm1", "account": str(self.home.id)}
        )
        (card,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.assertIs(card["undoable"], False)
        self.assertEqual(card["undo_reason"], undo.IRREVERSIBLE_DELETE)
        self.assertEqual(self.google.of("DELETE"), [])


class AfterTheApprovalTests(CardBase):
    """`resume_agent` runs the approved write: the result event, the ref and the chip."""

    def approve(self, tool, args, decision="approve"):
        run = AgentRun.objects.create(
            team_id=self.ctx.team_id,
            user_id=self.ctx.user_id,
            query="do it",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name=tool,
            arguments_json=args,
            summary=PENDING_APPROVAL_MARKER,
        )
        events = []
        with mock.patch.object(controller, "_drive_loop", return_value=None):
            resume_agent(run, decision, self.ctx, events.append)
        return events

    def of(self, events, kind):
        return [e for e in events if e["type"] == kind]

    def test_an_approved_update_names_the_event_and_where_it_is(self):
        events = self.approve("update_calendar_event", args_for(self.home))
        (result,) = self.of(events, "tool_call_result")
        self.assertEqual(
            result["calendar_event"],
            {
                "action": "update",
                "account_id": str(self.home.id),
                "calendar_id": "primary",
                "event_id": "hm1",
                "start": "2026-12-01T10:00:00+09:00",
                "end": "2026-12-01T11:00:00+09:00",
                "all_day": False,
            },
        )
        self.assertEqual(self.google.of("PATCH")[0].token, token_for(self.home))

    def test_an_approved_create_names_the_new_event(self):
        events = self.approve(
            "create_calendar_event", {**CREATE_ARGS, "account": str(self.home.id)}
        )
        (result,) = self.of(events, "tool_call_result")
        ref = result["calendar_event"]
        self.assertEqual(ref["action"], "create")
        self.assertEqual(ref["account_id"], str(self.home.id))
        self.assertTrue(ref["event_id"].startswith("new"))

    def test_an_approved_delete_says_deleted_and_has_no_time_to_name(self):
        events = self.approve(
            "delete_calendar_event", {"event_id": "hm1", "account": str(self.home.id)}
        )
        (result,) = self.of(events, "tool_call_result")
        self.assertEqual(
            result["calendar_event"],
            {
                "action": "delete",
                "account_id": str(self.home.id),
                "calendar_id": "primary",
                "event_id": "hm1",
                "deleted": True,
            },
        )

    def test_the_chip_arrives_on_a_sources_event_marked_operated(self):
        events = self.approve(
            "delete_calendar_event", {"event_id": "hm1", "account": str(self.home.id)}
        )
        (sources,) = self.of(events, "sources")
        (chip,) = sources["sources"]
        self.assertEqual(chip["entity_id"], f"calendar_event:{self.home.id}:hm1")
        self.assertTrue(chip["operated"])

    def test_a_rejection_changes_nothing_and_names_nothing(self):
        events = self.approve("delete_calendar_event", {"event_id": "hm1"}, decision="reject")
        self.assertEqual(self.google.of("DELETE"), [])
        self.assertEqual(self.of(events, "tool_call_result"), [])
        self.assertEqual(self.of(events, "sources"), [])

    def test_a_refused_account_is_an_error_event_and_no_ref(self):
        events = self.approve(
            "delete_calendar_event", {"event_id": "x", "account": str(self.stranger.id)}
        )
        self.assertEqual(self.google.calls, [])
        self.assertEqual(self.of(events, "tool_call_result"), [])
        (error,) = self.of(events, "tool_call_error")
        self.assertIn("not one of this member's", error["error"])

    def test_the_start_event_after_the_approval_makes_no_extra_read(self):
        self.approve("update_calendar_event", args_for(self.home))
        self.assertEqual(self.google.of("GET"), [])

    def test_with_the_flag_off_the_result_has_no_ref_and_no_chip(self):
        # An approval paused before the flag went off, naming no account: runs as it always did.
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: False}):
            events = self.approve("create_calendar_event", CREATE_ARGS)
        (result,) = self.of(events, "tool_call_result")
        self.assertNotIn("calendar_event", result)
        self.assertEqual(self.of(events, "sources"), [])

    def test_an_approval_paused_naming_an_account_and_approved_after_the_flag_went_off_is_refused(
        self,
    ):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: False}):
            events = self.approve(
                "create_calendar_event", {**CREATE_ARGS, "account": str(self.home.id)}
            )
        self.assertEqual(self.google.of("POST"), [])  # not on the work account either
        (error,) = self.of(events, "tool_call_error")
        self.assertIn("not available", error["error"])

"""A calendar write's time with no UTC offset and no zone (MIP-1059, `AGENT_PARITY_TOOLS`).

The tools are told to pass a naive "YYYY-MM-DDTHH:MM:SS" and the approval card says it is read in the member's
zone (`member_time_zone`, or the `time_zone` the model named). `build_time_endpoint` sent the value with
neither an offset nor a `timeZone` when the call had no `time_zone`. Google's rule (events.insert / patch,
`start.dateTime`): "A time zone offset is required unless a time zone is explicitly specified in `timeZone`",
so that request is rejected (HTTP 400), and a client that were read in the calendar's own zone would land the
event at another instant: the card would state a zone the event did not get.

What the app does in the same case: nothing to mirror. Its form turns the local time into an instant with
`toISOString()` (`CalendarEventModal.tsx`), so its body always carries a `Z`, and the app's endpoint passes the
client's `start` / `end` objects through to Google as they are. So the twin here compares the INSTANT each
leaves on an account whose calendar zone is not the member's, not the bytes of the body.

With the flag on the tool sends the member's zone for a naive time that names no zone; with it off the call is
what it always was (naive, no `timeZone`, rejected).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from rest_framework.test import APIClient

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent import controller
from origin.search_engine.agent.calendar import effective_time_zone, is_naive_datetime
from origin.search_engine.agent.tools import ToolError
from origin.search_engine.agent.tools.create_calendar_event import CREATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.update_calendar_event import UPDATE_CALENDAR_EVENT

from .calendar_fakes import token_for
from .notice_watch import watching_notices
from .test_agent_parity_calendar import CalendarBase

EVENTS = "/api/v2/calendar/events/"
TOKYO, LOS_ANGELES = "Asia/Tokyo", "America/Los_Angeles"


def at(zone: str, hour: int, day: int = 3) -> datetime:
    """`2026-12-<day> <hour>:00` in `zone`, as an instant."""
    return datetime(2026, 12, day, hour, 0, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def create_args(**over):
    return {
        "summary": "Design review",
        "start_iso": "2026-12-03T10:00:00",
        "end_iso": "2026-12-03T11:00:00",
        **over,
    }


def row_counts() -> dict[str, int]:
    return {m._meta.label: m._default_manager.count() for m in apps.get_models()}


class ZoneBase(CalendarBase):
    """The member's zone is Tokyo; the account's calendar, when it reads a naive time at all, is Los Angeles."""

    def setUp(self):
        super().setUp()
        cache.clear()
        CustomUser.objects.filter(id=self.user.id).update(timezone=TOKYO)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.google.naive_reading = LOS_ANGELES

    def held(self, account, event_id, calendar="primary"):
        return self.google.instants[(token_for(account), calendar, event_id)]

    def body_start(self):
        return self.google.of("POST")[-1].body["start"]


class ReadingTests(CalendarBase):
    def test_a_timed_value_without_an_offset_is_naive_and_the_others_are_not(self):
        for value in (
            "2026-12-03T10:00:00",
            "2026-12-03T10:00",
            "2026-12-03 10:00:00",
            "2026-12-03T10:00:00.250",
            "2026-12-03T10",
        ):
            self.assertTrue(is_naive_datetime(value), value)
        for value in (
            "2026-12-03T10:00:00Z",
            "2026-12-03T10:00:00z",
            "2026-12-03T10:00:00+09:00",
            "2026-12-03T10:00:00-08:00",
            "2026-12-03T10:00:00+0900",
            "2026-12-03T10:00:00+09",
            "2026-12-03T01:00:00.000Z",
            "2026-12-03",  # an all-day date is not a naive time
            None,
            42,
        ):
            self.assertFalse(is_naive_datetime(value), value)

    def test_the_date_part_of_a_naive_value_is_not_read_as_an_offset(self):
        # "-03" ends the DATE; only the time part may carry an offset.
        self.assertTrue(is_naive_datetime("2026-12-03T10:00:00"))
        self.assertTrue(is_naive_datetime("2026-12-03T00:00"))

    def test_the_zone_is_filled_only_for_a_naive_value_that_names_none_and_only_with_the_flag(self):
        uid = str(self.user.id)
        CustomUser.objects.filter(id=self.user.id).update(timezone=TOKYO)
        naive = ("2026-12-03T10:00:00", "2026-12-03T11:00:00")
        self.assertEqual(effective_time_zone(None, naive, uid), TOKYO)
        self.assertEqual(effective_time_zone(None, ("2026-12-03T10:00:00Z", naive[1]), uid), TOKYO)
        self.assertEqual(effective_time_zone("Europe/London", naive, uid), "Europe/London")
        self.assertIsNone(effective_time_zone(None, ("2026-12-03T10:00:00Z",) * 2, uid))
        self.assertIsNone(effective_time_zone(None, ("2026-12-03", "2026-12-04"), uid))


class WhatGoogleGetsTests(ZoneBase):
    def test_a_naive_time_with_no_zone_is_sent_in_the_members_zone(self):
        CREATE_CALENDAR_EVENT.run(create_args(), self.ctx)
        (post,) = self.google.of("POST")
        self.assertEqual(post.body["start"], {"dateTime": "2026-12-03T10:00:00", "timeZone": TOKYO})
        self.assertEqual(post.body["end"], {"dateTime": "2026-12-03T11:00:00", "timeZone": TOKYO})

    def test_the_zone_the_model_names_wins(self):
        CREATE_CALENDAR_EVENT.run(create_args(time_zone="Europe/London"), self.ctx)
        self.assertEqual(self.body_start()["timeZone"], "Europe/London")

    def test_a_value_that_carries_an_offset_is_left_as_it_is(self):
        CREATE_CALENDAR_EVENT.run(
            create_args(start_iso="2026-12-03T01:00:00Z", end_iso="2026-12-03T02:00:00Z"), self.ctx
        )
        self.assertEqual(self.body_start(), {"dateTime": "2026-12-03T01:00:00Z"})

    def test_an_all_day_event_has_no_zone(self):
        CREATE_CALENDAR_EVENT.run(
            create_args(start_iso="2026-12-25", end_iso="2026-12-26"), self.ctx
        )
        self.assertEqual(self.body_start(), {"date": "2026-12-25"})

    def test_a_member_with_no_zone_set_gets_the_servers_zone_the_card_names_too(self):
        CustomUser.objects.filter(id=self.user.id).update(timezone=None)
        CREATE_CALENDAR_EVENT.run(create_args(), self.ctx)
        self.assertEqual(self.body_start()["timeZone"], settings.TIME_ZONE)
        card = controller._friendly_arguments(
            create_args(), "create_calendar_event", ctx=self.ctx, card=True
        )
        self.assertEqual(card["member_time_zone"], settings.TIME_ZONE)

    def test_the_card_states_the_zone_the_tool_sends(self):
        card = controller._friendly_arguments(
            create_args(), "create_calendar_event", ctx=self.ctx, card=True
        )
        CREATE_CALENDAR_EVENT.run(create_args(), self.ctx)
        self.assertEqual(card["member_time_zone"], self.body_start()["timeZone"])

    def test_an_update_that_moves_an_event_reads_its_naive_times_in_the_members_zone_too(self):
        self.google.add_event(self.home, "primary", "hm1", summary="Dentist")
        UPDATE_CALENDAR_EVENT.run(
            {
                "event_id": "hm1",
                "account": str(self.home.id),
                "start_iso": "2026-12-04T15:00:00",
                "end_iso": "2026-12-04T16:00:00",
            },
            self.ctx,
        )
        (patch,) = self.google.of("PATCH")
        self.assertEqual(
            patch.body["start"], {"dateTime": "2026-12-04T15:00:00", "timeZone": TOKYO}
        )
        self.assertEqual(self.held(self.home, "hm1")[0], at(TOKYO, 15, day=4))

    def test_an_update_with_no_times_sends_none(self):
        self.google.add_event(self.home, "primary", "hm1", summary="Dentist")
        UPDATE_CALENDAR_EVENT.run(
            {"event_id": "hm1", "account": str(self.home.id), "summary": "Dentist!"}, self.ctx
        )
        self.assertNotIn("start", self.google.of("PATCH")[0].body)


class TwinTests(ZoneBase):
    """The app's endpoint and the tool, on an account whose calendar zone (Los Angeles) is not the member's
    (Tokyo): each leaves the same INSTANT, and nothing else differs."""

    def app_create(self):
        return self.client.post(
            EVENTS,
            {
                "account_id": str(self.home.id),
                "summary": "Design review",
                # what the app's form sends for 10:00 in Tokyo: `new Date(local).toISOString()`
                "start": {"dateTime": "2026-12-03T01:00:00.000Z"},
                "end": {"dateTime": "2026-12-03T02:00:00.000Z"},
            },
            format="json",
        )

    def tool_create(self):
        return CREATE_CALENDAR_EVENT.run(create_args(account=str(self.home.id)), self.ctx)

    def record(self, act):
        self.google.calls.clear()
        before = row_counts()
        with watching_notices() as notices:
            out = act()
        after = row_counts()
        return {
            "out": out,
            "calls": list(self.google.calls),
            "rows": {k: after[k] - before[k] for k in after if after[k] != before[k]},
            "notices": dict(notices),
        }

    def test_both_creates_leave_the_same_instant_on_a_calendar_in_another_zone(self):
        app = self.record(self.app_create)
        tool = self.record(self.tool_create)
        self.assertEqual(app["out"].status_code, 201)
        app_id = [k for k in self.google.instants if k[2] == "new1"][0]
        tool_id = [k for k in self.google.instants if k[2] == "new2"][0]
        self.assertEqual(self.google.instants[app_id], self.google.instants[tool_id])
        self.assertEqual(self.google.instants[tool_id], (at(TOKYO, 10), at(TOKYO, 11)))
        # ... and it is not the instant a read in the calendar's own zone would have given
        self.assertNotEqual(self.google.instants[tool_id][0], at(LOS_ANGELES, 10))
        # everything else is the same conversation: the call, the account, the params, the rows, the notices
        (a,), (t,) = app["calls"], tool["calls"]
        self.assertEqual(
            (a.method, a.path, a.params, a.token), (t.method, t.path, t.params, t.token)
        )
        self.assertEqual(
            {k: v for k, v in a.body.items() if k not in ("start", "end")},
            {k: v for k, v in t.body.items() if k not in ("start", "end")},
        )
        self.assertEqual(app["rows"], tool["rows"])
        self.assertEqual(app["notices"], tool["notices"])

    def test_both_updates_leave_the_same_instant(self):
        for event_id in ("a1", "b1"):
            self.google.add_event(self.home, "primary", event_id, summary="Dentist")
        app = self.record(
            lambda: self.client.patch(
                f"{EVENTS}a1/",
                {
                    "account_id": str(self.home.id),
                    "start": {"dateTime": "2026-12-04T06:00:00.000Z"},
                    "end": {"dateTime": "2026-12-04T07:00:00.000Z"},
                },
                format="json",
            )
        )
        tool = self.record(
            lambda: UPDATE_CALENDAR_EVENT.run(
                {
                    "event_id": "b1",
                    "account": str(self.home.id),
                    "start_iso": "2026-12-04T15:00:00",
                    "end_iso": "2026-12-04T16:00:00",
                },
                self.ctx,
            )
        )
        self.assertEqual(app["out"].status_code, 200)
        self.assertEqual(self.held(self.home, "a1"), self.held(self.home, "b1"))
        self.assertEqual(self.held(self.home, "b1"), (at(TOKYO, 15, day=4), at(TOKYO, 16, day=4)))
        self.assertEqual(app["rows"], tool["rows"])
        self.assertEqual(app["notices"], tool["notices"])

    def test_the_same_holds_when_google_rejects_a_zoneless_time_instead_of_reading_it(self):
        self.google.naive_reading = None
        self.tool_create()
        (key,) = self.google.instants
        self.assertEqual(self.google.instants[key], (at(TOKYO, 10), at(TOKYO, 11)))


class FlagOffTests(ZoneBase):
    """Flag off: the call is what it always was, and the zone is still the caller's to name."""

    flag = False

    def test_the_body_is_byte_for_byte_what_it_was_no_zone_added(self):
        self.google.naive_reading = None  # Google rejects it: the error is what a member got before
        with self.assertRaisesRegex(ToolError, "HTTP 400"):
            CREATE_CALENDAR_EVENT.run(create_args(), self.ctx)
        (post,) = self.google.of("POST")
        self.assertEqual(post.body["start"], {"dateTime": "2026-12-03T10:00:00"})
        self.assertEqual(post.body["end"], {"dateTime": "2026-12-03T11:00:00"})

    def test_where_google_reads_it_in_the_calendars_zone_the_event_lands_at_that_instant_as_before(
        self,
    ):
        CREATE_CALENDAR_EVENT.run(create_args(), self.ctx)
        (key,) = self.google.instants
        self.assertEqual(self.google.instants[key][0], at(LOS_ANGELES, 10))  # not 10:00 in Tokyo

    def test_a_zone_the_model_names_is_sent_as_before(self):
        CREATE_CALENDAR_EVENT.run(create_args(time_zone=TOKYO), self.ctx)
        self.assertEqual(self.body_start(), {"dateTime": "2026-12-03T10:00:00", "timeZone": TOKYO})

    def test_the_flag_off_helper_hands_back_what_it_was_given(self):
        self.assertIsNone(
            effective_time_zone(None, ("2026-12-03T10:00:00",) * 2, str(self.user.id))
        )

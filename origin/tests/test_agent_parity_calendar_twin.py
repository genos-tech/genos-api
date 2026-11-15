"""The twin tests of MIP-1059: the app's calendar endpoints against the tools', every channel.

"The tool does what the app does" is the contract, so it is proved the way #862, #867 and #868 proved theirs:
two IDENTICAL worlds (the same event on the member's second Google account, titled the same, under ids `a1`
and `b1`), the app's endpoint (`/api/v2/calendar/events/`, `account_id` in the body or the query) acting on
the first and the tool (`create_`, `update_`, `delete_calendar_event` with `account`, as the in-app agent's
approve path runs it) on the second. What each leaves is recorded on every channel and compared:

  * every call made to Google, with the token of the account it was made with (a recorder, never the network);
  * the event Google is left holding;
  * a row count of EVERY model (an agent write must not write anywhere the app's does not);
  * every way the app tells a person something (`watching_notices`: feed rows, inbox, chat mentions, e-mail,
    Web Push, webhooks).

A difference that is not listed in `KNOWN` fails the test. The known ones are asserted, not skipped, so they
cannot widen unseen.
"""

from __future__ import annotations

import copy

from django.apps import apps
from django.core.cache import cache
from rest_framework.test import APIClient

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.search_engine.agent.tools.create_calendar_event import CREATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.delete_calendar_event import DELETE_CALENDAR_EVENT
from origin.search_engine.agent.tools.update_calendar_event import UPDATE_CALENDAR_EVENT

from .calendar_fakes import token_for
from .notice_watch import watching_notices
from .test_agent_parity_calendar import CalendarBase

EVENTS = "/api/v2/calendar/events/"
APP, TOOL = "a1", "b1"
HOLIDAY = "en.japanese#holiday@group.v.calendar.google.com"

#: What the two are KNOWN to do differently (asserted below). Nothing else may differ. The tool takes a
#: `location`; the app's create endpoint has no such field and drops it. (The app also passes a client's
#: `recurrence` and an attendee's `displayName` through, which the tools have no argument for: nothing to
#: compare.)
KNOWN = {"location": "create sends `location` through (tool only)"}


def row_counts() -> dict[str, int]:
    return {model._meta.label: model._default_manager.count() for model in apps.get_models()}


def deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {label: after[label] - before[label] for label in after if after[label] != before[label]}


class TwinBase(CalendarBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        for event_id in (APP, TOOL):
            self.google.add_event(
                self.home,
                "primary",
                event_id,
                summary="Dentist",
                description="Teeth",
                attendees=[{"email": "dr@clinic.example"}],
            )

    # ---- the app and the tool --------------------------------------------------------------

    def tool(self, tool, args):
        """As the approve path runs it: the person acting, the write `via` the agent."""
        set_current_user(self.user, via={"kind": "agent"})
        try:
            return tool.run(args, self.ctx)
        finally:
            clear_current_user()

    # ---- recording ---------------------------------------------------------------------------

    def record(self, label, act):
        """Run `act` and return everything it did, on every channel."""
        self.google.calls.clear()
        before = row_counts()
        with watching_notices() as notices:
            out = act()
        return {
            "label": label,
            "out": out,
            "calls": list(self.google.calls),
            "rows": deltas(before, row_counts()),
            "notices": dict(notices),
        }

    def normalised(self, run, event_id):
        """A recorded run with this world's event id turned into a role, so two worlds compare."""
        calls = []
        for call in run["calls"]:
            body = copy.deepcopy(call.body)
            request = ((body or {}).get("conferenceData") or {}).get("createRequest")
            if request:
                # A fresh uuid each time (Google dedupes a retry on it): the one thing that must differ.
                self.assertRegex(request["requestId"], r"^[0-9a-f]{32}$")
                request["requestId"] = "{REQUEST}"
            calls.append(
                (
                    call.method,
                    call.raw_path.replace(event_id, "{EVENT}"),
                    call.params,
                    body,
                    call.token,
                )
            )
        return calls

    def assert_twins(self, app, tool, *, event_ids=(APP, TOOL)):
        self.assertEqual(
            self.normalised(app, event_ids[0]),
            self.normalised(tool, event_ids[1]),
            "the two Google conversations differ",
        )
        self.assertEqual(app["rows"], tool["rows"], "the two writes left different rows")
        self.assertEqual(app["notices"], tool["notices"], "the two told different people")

    def held(self, account, event_id, calendar="primary"):
        event = self.google.events.get((token_for(account), calendar, event_id))
        return None if event is None else {k: v for k, v in event.items() if k != "id"}


class UpdateTwinTests(TwinBase):
    def test_a_changed_title_and_time_on_the_second_account(self):
        change = {
            "summary": "Dentist (moved)",
            "start": {"dateTime": "2026-12-02T15:00:00", "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": "2026-12-02T16:00:00", "timeZone": "Asia/Tokyo"},
        }
        app = self.record(
            "app",
            lambda: self.client.patch(
                f"{EVENTS}{APP}/", {"account_id": str(self.home.id), **change}, format="json"
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                UPDATE_CALENDAR_EVENT,
                {
                    "event_id": TOOL,
                    "account": str(self.home.id),
                    "summary": "Dentist (moved)",
                    "start_iso": "2026-12-02T15:00:00",
                    "end_iso": "2026-12-02T16:00:00",
                    "time_zone": "Asia/Tokyo",
                },
            ),
        )
        self.assertEqual(app["out"].status_code, 200)
        self.assert_twins(app, tool)
        self.assertEqual(self.held(self.home, APP), self.held(self.home, TOOL))
        # Only the second account's token was ever used, by either.
        self.assertEqual({c.token for c in app["calls"] + tool["calls"]}, {token_for(self.home)})

    def test_a_replaced_guest_list_and_a_meet_link(self):
        app = self.record(
            "app",
            lambda: self.client.patch(
                f"{EVENTS}{APP}/",
                {
                    "account_id": str(self.home.id),
                    "attendees": [{"email": "a@x.example"}, {"email": "b@x.example"}],
                    "add_meet": True,
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                UPDATE_CALENDAR_EVENT,
                {
                    "event_id": TOOL,
                    "account": str(self.home.id),
                    "attendees": ["a@x.example", "b@x.example"],
                    "add_meet": True,
                },
            ),
        )
        self.assert_twins(app, tool)
        # The guests are told by Google, never by Genos: `sendUpdates=none`, and nobody here is told.
        patch = tool["calls"][0]
        self.assertEqual(patch.params["sendUpdates"], "none")
        self.assertEqual(tool["notices"]["rows"], {})
        self.assertEqual(tool["notices"]["emails"], 0)
        self.assertEqual(tool["notices"]["pushes"], {})

    def test_clearing_the_guests_and_removing_the_meet_link(self):
        app = self.record(
            "app",
            lambda: self.client.patch(
                f"{EVENTS}{APP}/",
                {"account_id": str(self.home.id), "attendees": [], "add_meet": False},
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                UPDATE_CALENDAR_EVENT,
                {
                    "event_id": TOOL,
                    "account": str(self.home.id),
                    "attendees": [],
                    "add_meet": False,
                },
            ),
        )
        self.assert_twins(app, tool)
        self.assertEqual(self.held(self.home, APP), self.held(self.home, TOOL))
        self.assertEqual(self.held(self.home, TOOL)["attendees"], [])

    def test_a_calendar_whose_id_has_a_hash(self):
        for event_id in (APP, TOOL):
            self.google.add_event(self.home, HOLIDAY, event_id, summary="Culture Day")
        app = self.record(
            "app",
            lambda: self.client.patch(
                f"{EVENTS}{APP}/",
                {
                    "account_id": str(self.home.id),
                    "calendar_id": HOLIDAY,
                    "summary": "Culture Day!",
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                UPDATE_CALENDAR_EVENT,
                {
                    "event_id": TOOL,
                    "calendar_id": HOLIDAY,
                    "account": str(self.home.id),
                    "summary": "Culture Day!",
                },
            ),
        )
        self.assert_twins(app, tool)
        self.assertEqual(self.held(self.home, APP, HOLIDAY), self.held(self.home, TOOL, HOLIDAY))
        self.assertIn("%23", tool["calls"][0].raw_path)

    def test_the_default_account_when_none_is_named(self):
        for event_id in (APP, TOOL):
            self.google.add_event(self.work, "primary", event_id, summary="Standup")
        app = self.record(
            "app",
            lambda: self.client.patch(f"{EVENTS}{APP}/", {"summary": "Standup!"}, format="json"),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(UPDATE_CALENDAR_EVENT, {"event_id": TOOL, "summary": "Standup!"}),
        )
        self.assert_twins(app, tool)
        self.assertEqual({c.token for c in app["calls"] + tool["calls"]}, {token_for(self.work)})

    def test_an_account_that_is_not_the_members_is_refused_by_both_and_nothing_is_sent(self):
        app = self.record(
            "app",
            lambda: self.client.patch(
                f"{EVENTS}{APP}/",
                {"account_id": str(self.stranger.id), "summary": "x"},
                format="json",
            ),
        )
        self.assertEqual(app["out"].status_code, 404)
        self.assertEqual(app["calls"], [])
        from origin.search_engine.agent.tools import ToolError  # noqa: PLC0415

        with self.assertRaises(ToolError):
            self.tool(
                UPDATE_CALENDAR_EVENT,
                {"event_id": TOOL, "account": str(self.stranger.id), "summary": "x"},
            )
        self.assertEqual(self.google.calls, [])


class CreateTwinTests(TwinBase):
    def test_a_new_event_with_guests_and_a_meet_link_on_the_second_account(self):
        app = self.record(
            "app",
            lambda: self.client.post(
                EVENTS,
                {
                    "account_id": str(self.home.id),
                    "summary": "Design review",
                    "start": {"dateTime": "2026-12-03T10:00:00", "timeZone": "Asia/Tokyo"},
                    "end": {"dateTime": "2026-12-03T11:00:00", "timeZone": "Asia/Tokyo"},
                    "attendees": [{"email": "a@x.example"}],
                    "add_meet": True,
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                CREATE_CALENDAR_EVENT,
                {
                    "account": str(self.home.id),
                    "summary": "Design review",
                    "start_iso": "2026-12-03T10:00:00",
                    "end_iso": "2026-12-03T11:00:00",
                    "time_zone": "Asia/Tokyo",
                    "attendees": ["a@x.example"],
                    "add_meet": True,
                },
            ),
        )
        self.assertEqual(app["out"].status_code, 201)
        self.assert_twins(app, tool, event_ids=("new1", "new2"))
        self.assertEqual({c.token for c in app["calls"] + tool["calls"]}, {token_for(self.home)})

    def test_an_all_day_event(self):
        app = self.record(
            "app",
            lambda: self.client.post(
                EVENTS,
                {
                    "account_id": str(self.home.id),
                    "summary": "Holiday",
                    "start": {"date": "2026-12-25"},
                    "end": {"date": "2026-12-26"},
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                CREATE_CALENDAR_EVENT,
                {
                    "account": str(self.home.id),
                    "summary": "Holiday",
                    "start_iso": "2026-12-25",
                    "end_iso": "2026-12-26",
                },
            ),
        )
        self.assert_twins(app, tool, event_ids=("new1", "new2"))

    def test_the_default_account_when_none_is_named(self):
        app = self.record(
            "app",
            lambda: self.client.post(
                EVENTS,
                {
                    "summary": "Lunch",
                    "start": {"dateTime": "2026-12-03T12:00:00", "timeZone": "Asia/Tokyo"},
                    "end": {"dateTime": "2026-12-03T13:00:00", "timeZone": "Asia/Tokyo"},
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                CREATE_CALENDAR_EVENT,
                {
                    "summary": "Lunch",
                    "start_iso": "2026-12-03T12:00:00",
                    "end_iso": "2026-12-03T13:00:00",
                    "time_zone": "Asia/Tokyo",
                },
            ),
        )
        self.assert_twins(app, tool, event_ids=("new1", "new2"))
        self.assertEqual({c.token for c in app["calls"] + tool["calls"]}, {token_for(self.work)})

    def test_the_known_difference_the_tool_sends_a_location_and_the_app_does_not(self):
        app = self.record(
            "app",
            lambda: self.client.post(
                EVENTS,
                {
                    "account_id": str(self.home.id),
                    "summary": "Lunch",
                    "location": "Cafe",
                    "start": {"dateTime": "2026-12-03T12:00:00", "timeZone": "Asia/Tokyo"},
                    "end": {"dateTime": "2026-12-03T13:00:00", "timeZone": "Asia/Tokyo"},
                },
                format="json",
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                CREATE_CALENDAR_EVENT,
                {
                    "account": str(self.home.id),
                    "summary": "Lunch",
                    "location": "Cafe",
                    "start_iso": "2026-12-03T12:00:00",
                    "end_iso": "2026-12-03T13:00:00",
                    "time_zone": "Asia/Tokyo",
                },
            ),
        )
        self.assertNotIn("location", app["calls"][0].body)
        self.assertEqual(tool["calls"][0].body.pop("location"), "Cafe")
        self.assert_twins(app, tool, event_ids=("new1", "new2"))
        self.assertIn("location", KNOWN)


class DeleteTwinTests(TwinBase):
    def test_a_delete_on_the_second_account(self):
        app = self.record(
            "app",
            lambda: self.client.delete(
                f"{EVENTS}{APP}/?account_id={self.home.id}&calendar_id=primary"
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                DELETE_CALENDAR_EVENT, {"event_id": TOOL, "account": str(self.home.id)}
            ),
        )
        self.assertEqual(app["out"].status_code, 204)
        self.assert_twins(app, tool)
        self.assertIsNone(self.held(self.home, APP))
        self.assertIsNone(self.held(self.home, TOOL))

    def test_deleting_what_is_already_gone_succeeds_for_both(self):
        app = self.record(
            "app",
            lambda: self.client.delete(
                f"{EVENTS}nope/?account_id={self.home.id}&calendar_id=primary"
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                DELETE_CALENDAR_EVENT, {"event_id": "nope", "account": str(self.home.id)}
            ),
        )
        self.assertEqual(app["out"].status_code, 204)
        self.assertTrue(tool["out"]["already_gone"])
        self.assert_twins(app, tool, event_ids=("nope", "nope"))

    def test_a_delete_names_the_account_that_is_asked_not_the_other_one(self):
        # The same event id on both accounts: each delete touches only the account it names.
        self.google.add_event(self.work, "primary", TOOL, summary="Same id, other account")
        self.tool(DELETE_CALENDAR_EVENT, {"event_id": TOOL, "account": str(self.home.id)})
        self.assertIsNone(self.held(self.home, TOOL))
        self.assertIsNotNone(self.held(self.work, TOOL))

    def test_no_row_in_any_model_is_written_by_either(self):
        app = self.record(
            "app",
            lambda: self.client.delete(
                f"{EVENTS}{APP}/?account_id={self.home.id}&calendar_id=primary"
            ),
        )
        tool = self.record(
            "tool",
            lambda: self.tool(
                DELETE_CALENDAR_EVENT, {"event_id": TOOL, "account": str(self.home.id)}
            ),
        )
        self.assertEqual(app["rows"], {})
        self.assertEqual(tool["rows"], {})
        self.assertEqual(tool["notices"], {"rows": {}, "emails": 0, "pushes": {}})

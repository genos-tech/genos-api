"""`resolve_remind_at` — "remind me …" as a model says it, as one instant (MIP-884).

The model is told today's date and never the time, and may not know the
user's zone, so the server resolves the words. Each case below is a way that
could silently set a reminder for the wrong moment, which is worse than
refusing: the user trusts a reminder precisely because they stop thinking
about the thing.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from origin.services.reminder_input import (
    MAX_MINUTES,
    ReminderInputError,
    resolve_remind_at,
)

UTC = dt_timezone.utc
TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")
# 12:00 on 2 November 2026 in Tokyo.
NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


def resolve(zone=TOKYO, **kw):
    return resolve_remind_at(zone=zone, now=NOW, **kw)


class RemindAtTests(SimpleTestCase):
    def test_a_local_date_time_is_read_in_the_users_zone(self):
        self.assertEqual(
            resolve(remind_at="2026-11-06T09:30"), datetime(2026, 11, 6, 0, 30, tzinfo=UTC)
        )

    def test_the_same_words_are_a_different_instant_in_another_zone(self):
        self.assertEqual(
            resolve(NEW_YORK, remind_at="2026-11-06T09:00"),
            datetime(2026, 11, 6, 14, 0, tzinfo=UTC),
        )

    def test_daylight_saving_follows_the_date_not_the_zone_name(self):
        in_summer = resolve_remind_at(
            zone=NEW_YORK, now=datetime(2026, 6, 1, tzinfo=UTC), remind_at="2026-07-01T09:00"
        )
        self.assertEqual(in_summer, datetime(2026, 7, 1, 13, 0, tzinfo=UTC))

    def test_a_bare_date_means_nine_in_the_morning_not_midnight(self):
        """Python 3.11 reads "2026-11-06" as midnight; "remind me Friday"
        must not become a 00:00 nudge."""
        self.assertEqual(resolve(remind_at="2026-11-06"), datetime(2026, 11, 6, 0, 0, tzinfo=UTC))
        self.assertEqual(
            resolve(remind_at="2026-11-06").astimezone(TOKYO),
            datetime(2026, 11, 6, 9, tzinfo=TOKYO),
        )

    def test_an_instant_that_carries_its_offset_is_taken_as_given(self):
        want = datetime(2026, 11, 6, 3, 30, tzinfo=UTC)
        for text in ("2026-11-06T09:00:00+05:30", "2026-11-06T03:30:00Z", "2026-11-06T12:30+09:00"):
            self.assertEqual(resolve(NEW_YORK, remind_at=text), want, text)

    def test_a_space_instead_of_a_T_is_accepted(self):
        self.assertEqual(
            resolve(remind_at="2026-11-06 09:00"), datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
        )

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(
            resolve(remind_at="  2026-11-06T09:00 "), resolve(remind_at="2026-11-06T09:00")
        )

    def test_text_that_is_not_a_time_is_refused(self):
        for text in ("tomorrow morning", "9am", "2026-13-45", "2026-11-06T25:00", "soon"):
            with self.assertRaises(ReminderInputError, msg=text) as caught:
                resolve(remind_at=text)
            self.assertIn("`remind_at`", str(caught.exception), text)

    def test_a_time_that_has_passed_is_refused_and_the_error_says_what_time_it_is(self):
        with self.assertRaises(ReminderInputError) as caught:
            resolve(remind_at="2026-11-02T09:00")
        message = str(caught.exception)
        self.assertIn("already passed", message)
        self.assertIn("It is now 2026-11-02 12:00 (Asia/Tokyo)", message)

    def test_the_present_instant_is_already_passed(self):
        with self.assertRaises(ReminderInputError):
            resolve(remind_at=NOW.isoformat())
        resolve(remind_at=(NOW + timedelta(seconds=1)).isoformat())

    def test_the_endpoints_two_minutes_of_slack_is_not_extended(self):
        with self.assertRaises(ReminderInputError):
            resolve(remind_at=(NOW - timedelta(minutes=1)).isoformat())

    def test_a_year_out_is_the_limit(self):
        resolve(remind_at=(NOW + timedelta(days=364)).isoformat())
        with self.assertRaises(ReminderInputError) as caught:
            resolve(remind_at=(NOW + timedelta(days=366)).isoformat())
        self.assertIn("`remind_at` must be within a year", str(caught.exception))

    def test_nothing_asked_for_is_none(self):
        self.assertIsNone(resolve())
        self.assertIsNone(resolve(remind_at=""))
        self.assertIsNone(resolve(remind_at=None, remind_in_minutes=None))


class RemindInMinutesTests(SimpleTestCase):
    def test_minutes_are_counted_from_now(self):
        self.assertEqual(resolve(remind_in_minutes=90), NOW + timedelta(minutes=90))

    def test_a_whole_number_in_another_spelling_is_accepted(self):
        for value in ("30", 30.0, " 30 "):
            self.assertEqual(resolve(remind_in_minutes=value), NOW + timedelta(minutes=30), value)

    def test_not_a_positive_whole_number_is_refused(self):
        for value in (0, -1, 1.5, True, False, "soon", "", float("nan"), float("inf"), [], {}):
            with self.assertRaises(ReminderInputError, msg=repr(value)) as caught:
                resolve(remind_in_minutes=value)
            self.assertIn("`remind_in_minutes`", str(caught.exception), repr(value))

    def test_a_year_of_minutes_is_the_limit(self):
        self.assertEqual(MAX_MINUTES, 365 * 24 * 60)
        resolve(remind_in_minutes=MAX_MINUTES)
        with self.assertRaises(ReminderInputError) as caught:
            resolve(remind_in_minutes=MAX_MINUTES + 1)
        self.assertIn("at most", str(caught.exception))
        with self.assertRaises(ReminderInputError):
            resolve(remind_in_minutes=10**15)

    def test_both_at_once_is_refused(self):
        with self.assertRaises(ReminderInputError) as caught:
            resolve(remind_at="2026-11-06", remind_in_minutes=5)
        self.assertIn("not both", str(caught.exception))

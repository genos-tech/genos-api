"""Tests for the Slack-style "pause notifications" predicate.

`is_snoozed` is the single server-side gate consulted by BOTH `should_push`
and `should_email` (asserted in test_webpush.py / test_email_gating.py). Here
we pin the predicate itself and the zone-resolution rule that is the feature's
load-bearing correctness detail — a recurring window must fire in the zone the
user's client used (`current_location or timezone`), NOT `user_time.user_zone`
(which reads only `.timezone`).
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.utils import timezone

from origin.models.common.notification_models import NotificationPreference
from origin.services.notification_snooze import is_snoozed, resolve_snooze_zone
from origin.tests.test_base import BaseAPITestCase


class ResolveSnoozeZoneTests(BaseAPITestCase):
    def test_current_location_wins_over_timezone(self):
        # The regression that pins the whole feature: a manual location must
        # beat the browser-derived timezone, matching the frontend resolver.
        self.user.timezone = "America/New_York"
        self.user.current_location = "Asia/Tokyo"
        self.user.save(update_fields=["timezone", "current_location"])
        self.assertEqual(resolve_snooze_zone(self.user), ZoneInfo("Asia/Tokyo"))

    def test_timezone_used_when_no_location(self):
        self.user.timezone = "Europe/London"
        self.user.current_location = None
        self.user.save(update_fields=["timezone", "current_location"])
        self.assertEqual(resolve_snooze_zone(self.user), ZoneInfo("Europe/London"))

    def test_falls_back_when_neither_set(self):
        self.user.timezone = None
        self.user.current_location = None
        self.user.save(update_fields=["timezone", "current_location"])
        # settings.TIME_ZONE is UTC; resolve_zone never raises.
        self.assertEqual(resolve_snooze_zone(self.user), ZoneInfo("UTC"))


class IsSnoozedOneShotTests(BaseAPITestCase):
    def _prefs(self, **kwargs):
        return NotificationPreference.objects.create(user=self.user2, **kwargs)

    def test_no_snooze_is_not_snoozed(self):
        self.assertFalse(is_snoozed(self._prefs()))

    def test_none_prefs_is_not_snoozed(self):
        self.assertFalse(is_snoozed(None))

    def test_future_until_is_snoozed(self):
        prefs = self._prefs(snooze_until=timezone.now() + timedelta(hours=1))
        self.assertTrue(is_snoozed(prefs))

    def test_past_until_is_not_snoozed(self):
        prefs = self._prefs(snooze_until=timezone.now() - timedelta(minutes=1))
        self.assertFalse(is_snoozed(prefs))

    def test_until_is_evaluated_against_passed_now(self):
        at = datetime(2026, 8, 10, 12, 0, tzinfo=dt_timezone.utc)
        prefs = self._prefs(snooze_until=datetime(2026, 8, 10, 13, 0, tzinfo=dt_timezone.utc))
        self.assertTrue(is_snoozed(prefs, now=at))
        self.assertFalse(is_snoozed(prefs, now=at + timedelta(hours=2)))


class IsSnoozedScheduleTests(BaseAPITestCase):
    """Recurring daily window, evaluated in the user's resolved zone."""

    def setUp(self):
        super().setUp()
        # Pin the user to Tokyo via a manual location so the zone rule is
        # exercised (and would break if the code used `.timezone` alone).
        self.user2.current_location = "Asia/Tokyo"
        self.user2.timezone = "America/New_York"
        self.user2.save(update_fields=["current_location", "timezone"])

    def _prefs(self, schedule):
        return NotificationPreference.objects.create(user=self.user2, snooze_schedule=schedule)

    def _at_tokyo(self, hour, minute=0):
        # A UTC instant that is `hour:minute` local time in Tokyo (UTC+9).
        local = datetime(2026, 8, 10, hour, minute, tzinfo=ZoneInfo("Asia/Tokyo"))
        return local.astimezone(dt_timezone.utc)

    def test_disabled_schedule_never_snoozes(self):
        prefs = self._prefs({"enabled": False, "start": "00:00", "end": "23:59"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(12)))

    def test_same_day_window_inside(self):
        prefs = self._prefs({"enabled": True, "start": "09:00", "end": "17:00"})
        self.assertTrue(is_snoozed(prefs, now=self._at_tokyo(12)))

    def test_same_day_window_outside(self):
        prefs = self._prefs({"enabled": True, "start": "09:00", "end": "17:00"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(18)))

    def test_same_day_end_is_exclusive(self):
        prefs = self._prefs({"enabled": True, "start": "09:00", "end": "17:00"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(17)))

    def test_overnight_window_late_evening(self):
        # 17:00–09:00 spans midnight: 23:00 is inside.
        prefs = self._prefs({"enabled": True, "start": "17:00", "end": "09:00"})
        self.assertTrue(is_snoozed(prefs, now=self._at_tokyo(23)))

    def test_overnight_window_early_morning(self):
        prefs = self._prefs({"enabled": True, "start": "17:00", "end": "09:00"})
        self.assertTrue(is_snoozed(prefs, now=self._at_tokyo(6)))

    def test_overnight_window_midday_gap(self):
        prefs = self._prefs({"enabled": True, "start": "17:00", "end": "09:00"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(12)))

    def test_zone_matters_wrong_zone_would_flip(self):
        # At 12:00 Tokyo it is 23:00 the previous day in New York. An
        # overnight 17:00–09:00 window is INACTIVE at 12:00 Tokyo but ACTIVE
        # at 23:00 NY — so if the code resolved the zone from `.timezone`
        # (New York) this would wrongly be snoozed. Assert it is not.
        prefs = self._prefs({"enabled": True, "start": "17:00", "end": "09:00"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(12)))

    def test_equal_start_end_is_never(self):
        prefs = self._prefs({"enabled": True, "start": "09:00", "end": "09:00"})
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(9)))
        self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(12)))

    def test_malformed_schedule_degrades_to_not_snoozed(self):
        for bad in (
            {"enabled": True, "start": "9am", "end": "5pm"},
            {"enabled": True, "start": "25:00", "end": "09:00"},
            {"enabled": True, "start": "17:00"},  # missing end
            {"enabled": True},
            "not-a-dict",
        ):
            prefs = NotificationPreference(user=self.user2, snooze_schedule=bad)
            self.assertFalse(is_snoozed(prefs, now=self._at_tokyo(23)), bad)

    def test_one_shot_wins_even_outside_schedule(self):
        # A one-shot pause is an explicit override; it snoozes regardless of
        # the (inactive) schedule window.
        prefs = self._prefs({"enabled": True, "start": "09:00", "end": "17:00"})
        prefs.snooze_until = self._at_tokyo(18) + timedelta(hours=1)
        prefs.save(update_fields=["snooze_until"])
        self.assertTrue(is_snoozed(prefs, now=self._at_tokyo(18)))

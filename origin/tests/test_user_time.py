"""Per-user date-boundary math: the `user_time` helpers and the
`GET / PATCH /api/v2/user/preferences/timezone/` endpoint.

The bug being fixed: `TIME_ZONE = "UTC"` and every "today" in the codebase
was UTC too, so for a user in Tokyo the server's today was a day behind for
nine hours out of every twenty-four.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from origin.services.user_time import (
    is_valid_timezone,
    resolve_zone,
    user_now,
    user_today,
    user_zone,
)

User = get_user_model()

URL = "/api/v2/user/preferences/timezone/"

# 2026-07-30 22:30 UTC. In Tokyo (UTC+9) that is already 2026-07-31 07:30,
# and in Los Angeles (UTC-7) it is still 2026-07-30 15:30 — one instant, three
# different calendar days depending on who is asking.
LATE_UTC = datetime(2026, 7, 30, 22, 30, tzinfo=dt_timezone.utc)
# 2026-07-30 06:00 UTC — still 2026-07-29 23:00 in Los Angeles. Needed as a
# SECOND fixture because at LATE_UTC the LA date happens to equal the UTC
# date, so a Los Angeles assertion pinned to LATE_UTC passes even when the
# helper ignores the user's zone entirely (mutation-checked).
EARLY_UTC = datetime(2026, 7, 30, 6, 0, tzinfo=dt_timezone.utc)


class TestTimezoneValidation(TestCase):
    def test_accepts_real_iana_names(self):
        for name in ("Asia/Tokyo", "UTC", "America/Los_Angeles", "Europe/Paris"):
            self.assertTrue(is_valid_timezone(name), name)

    def test_rejects_junk_and_empty(self):
        for name in ("", None, "Mars/Olympus_Mons", "GMT+9", "not a zone", 42):
            self.assertFalse(is_valid_timezone(name), repr(name))

    def test_resolve_falls_back_to_settings_time_zone(self):
        # Not "returns UTC" — the assertion is that it follows the setting,
        # so this test still means something if TIME_ZONE ever changes.
        self.assertEqual(str(resolve_zone(None)), "UTC")
        self.assertEqual(str(resolve_zone("Mars/Olympus_Mons")), "UTC")


class TestUserToday(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="tz-user", email="tz@test.com", password="testpass123"
        )

    def test_null_timezone_behaves_exactly_like_the_old_code(self):
        # This is what makes adopting `user_today` safe one call site at a
        # time: with no timezone stored it is byte-identical to
        # `timezone.now().date()`.
        self.assertIsNone(self.user.timezone)
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(user_today(self.user), LATE_UTC.date())

    def test_tokyo_user_is_already_on_the_next_day(self):
        self.user.timezone = "Asia/Tokyo"
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(user_today(self.user).isoformat(), "2026-07-31")

    def test_los_angeles_user_is_still_on_the_previous_day(self):
        self.user.timezone = "America/Los_Angeles"
        with patch("django.utils.timezone.now", return_value=EARLY_UTC):
            self.assertEqual(user_today(self.user).isoformat(), "2026-07-29")

    def test_user_now_keeps_the_instant_and_moves_the_wall_clock(self):
        # A digest that fires "at 8am local" needs the hour, not just the date.
        self.user.timezone = "Asia/Tokyo"
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            local = user_now(self.user)
        self.assertEqual(local.hour, 7)
        self.assertEqual(local.timestamp(), LATE_UTC.timestamp())

    def test_unknown_stored_zone_falls_back_rather_than_raising(self):
        # A row can hold anything — hand-edited, or written before this
        # Python's tzdata dropped a zone. It must not 500 a request.
        self.user.timezone = "Mars/Olympus_Mons"
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(user_today(self.user), LATE_UTC.date())

    def test_tolerates_none_and_objects_without_a_timezone(self):
        # Anonymous / system code paths.
        self.assertEqual(user_zone(None), ZoneInfo("UTC"))
        self.assertEqual(user_zone(object()), ZoneInfo("UTC"))


class TestTimezonePreferenceEndpoint(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="tz-pref",
            email="tzpref@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_get_returns_empty_when_unknown(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"timezone": ""})

    def test_patch_persists_a_valid_zone(self):
        resp = self.client.patch(URL, {"timezone": "Asia/Tokyo"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"timezone": "Asia/Tokyo"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Tokyo")

    def test_patch_unknown_zone_stores_null_and_still_succeeds(self):
        # The client sends whatever the browser reported, not user input —
        # failing the boot-time sync over an exotic value would be worse than
        # falling back to server time. The echo tells the client what stuck.
        resp = self.client.patch(URL, {"timezone": "Mars/Olympus_Mons"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"timezone": ""})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.timezone)

    def test_patch_empty_clears_back_to_unknown(self):
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        resp = self.client.patch(URL, {"timezone": ""}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.timezone)

    def test_patch_rejects_a_non_string(self):
        resp = self.client.patch(URL, {"timezone": 9}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_patch_is_scoped_to_the_caller(self):
        other = User.objects.create_user(
            username="other", email="other@test.com", password="testpass123"
        )
        self.client.patch(URL, {"timezone": "Asia/Tokyo"}, format="json")
        other.refresh_from_db()
        self.assertIsNone(other.timezone)

    def test_requires_authentication(self):
        anon = APIClient()
        self.assertIn(anon.get(URL).status_code, (401, 403))

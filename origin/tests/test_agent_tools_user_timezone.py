"""Agent tools resolve "today" in the ASKING USER's timezone.

The symptom this closes is the one PRODUCT_READINESS_PLAN §3.4 leads with:
ask Spotlight "what's due today" at 9am JST and it answered for yesterday,
because every tool called `timezone.now().date()` and `TIME_ZONE` is UTC.

Agent tools are personal scope even when the DATA is team-wide — a tool
result is composed into one person's answer and never cached across users —
so `get_team_task_summary` gets the same treatment as `get_my_focus_tasks`.
See `services/user_time.py`.
"""

import re
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.services.user_time import today_for_user_id, zone_for_user_id

User = get_user_model()

TOOLS_DIR = Path(__file__).resolve().parent.parent / "search_engine" / "agent" / "tools"

# 2026-07-30 22:30 UTC = 2026-07-31 07:30 in Tokyo. The instant where a
# UTC-based "today" is a day behind a Tokyo user's.
LATE_UTC = datetime(2026, 7, 30, 22, 30, tzinfo=dt_timezone.utc)


class TestTodayForUserId(TestCase):
    """The helper the tools call, since they hold `ctx.user_id`, not a row."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="tz-tool", email="tztool@test.com", password="testpass123"
        )

    def test_uses_the_users_zone(self):
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(today_for_user_id(self.user.pk).isoformat(), "2026-07-31")

    def test_falls_back_to_server_time_when_unknown(self):
        # Every row is NULL until the client reports one, so this is the
        # common path and must match the old behaviour exactly.
        self.assertIsNone(self.user.timezone)
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(today_for_user_id(self.user.pk), LATE_UTC.date())

    def test_tolerates_a_missing_or_empty_user_id(self):
        # Tools are server-invoked, but a deleted user mid-run must not 500.
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            self.assertEqual(today_for_user_id(None), LATE_UTC.date())
            self.assertEqual(
                today_for_user_id("00000000-0000-0000-0000-000000000000"),
                LATE_UTC.date(),
            )

    def test_zone_lookup_reads_the_stored_value_not_a_cache(self):
        # A user who fixes their timezone should see it on the next question,
        # so this must not be memoised per process.
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.assertEqual(str(zone_for_user_id(self.user.pk)), "Asia/Tokyo")
        self.user.timezone = "Europe/Paris"
        self.user.save(update_fields=["timezone"])
        self.assertEqual(str(zone_for_user_id(self.user.pk)), "Europe/Paris")


class TestNoToolComputesServerToday(TestCase):
    """No agent tool may derive a calendar day from server time.

    A source-level assertion because there is no single seam every tool
    passes through — each one computes its own `today`. Without this, a new
    tool reintroduces the bug and nothing notices: the eval suite asserts
    answers, not which clock produced them.
    """

    # `timezone.now()` on its own is fine — a duration window
    # (`now - timedelta(days=30)`) has no day boundary in it. These are the
    # spellings that DO pin a calendar day.
    FORBIDDEN = (
        re.compile(r"timezone\.now\(\)\.date\(\)"),
        re.compile(r"timezone\.localdate\(\)"),
        re.compile(r"\bdate\.today\(\)"),
        re.compile(r"datetime\.now\(\)\.date\(\)"),
    )

    def test_no_tool_pins_a_day_to_server_time(self):
        offenders = []
        for path in sorted(TOOLS_DIR.glob("*.py")):
            source = path.read_text()
            for pattern in self.FORBIDDEN:
                for match in pattern.finditer(source):
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line} {match.group(0)}")
        self.assertEqual(
            offenders,
            [],
            "These pin a calendar day to server time; use "
            "`today_for_user_id(ctx.user_id)` (or `zone_for_user_id`) instead:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_guard_would_actually_catch_something(self):
        # Proves the patterns match the code they are meant to reject —
        # otherwise a typo'd regex makes the test above vacuously green.
        sample = "today = timezone.now().date()\nd = timezone.localdate()\n"
        hits = [p.pattern for p in self.FORBIDDEN if p.search(sample)]
        self.assertEqual(len(hits), 2, hits)

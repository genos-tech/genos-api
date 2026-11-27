"""Dashboard pages and "my pace" count a close on the member's own day, not UTC's (MIP-2438).

One close is placed where the two disagree: 08:00 in Japan is the evening before in UTC, and
20:00 in Los Angeles is the next morning in UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from origin.search_engine.agent.tools.get_my_throughput import GET_MY_THROUGHPUT
from origin.services.schedule_jobs import ai_dashboard
from origin.tests.test_schedule_jobs import JobBase

DAY = date(2026, 6, 16)  # a Tuesday; its week starts on Monday the 15th


class LocalDayTests(JobBase):
    def close_at(self, zone_name: str, day: date, hour: int):
        """A member in `zone_name`, a task they closed on `day` at `hour`, and "now" a day later."""
        zone = ZoneInfo(zone_name)
        self.user.timezone = zone_name
        self.user.save(update_fields=["timezone"])
        self.now = datetime(day.year, day.month, day.day, 15, tzinfo=zone) + timedelta(days=1)
        task = self.make_task(f"Closed in {zone_name}", status="Closed")
        at = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
        self.assertNotEqual(at.astimezone(ZoneInfo("UTC")).date(), day)
        self.activity(task, self.user, "status_changed", "WIP", "Closed", at=at)

    def at_now(self):
        return mock.patch("django.utils.timezone.now", return_value=self.now)

    def closed_days(self, points, label="label"):
        return [(p[label], p["closed"]) for p in points if p["closed"]]

    def check(self, zone_name: str, hour: int):
        self.close_at(zone_name, DAY, hour)
        page = self.run_job(
            "ai_dashboard", kind="project", project_id=self.project.project_id, period="week"
        )["dashboard"]
        self.assertEqual(self.closed_days(page["series"]), [(DAY.isoformat(), 1)])

        mine = self.run_job("ai_dashboard", kind="mine", period="week")["dashboard"]
        self.assertEqual(self.closed_days(mine["series"]), [(DAY.isoformat(), 1)])

        with self.at_now():
            tool = GET_MY_THROUGHPUT.run({"days": 7, "bucket": "day"}, self.ctx)
            # A page that others read counts "mine" with its own query.
            shared = ai_dashboard._mine(self.ctx, 7, "day", {str(self.user.id), str(self.user2.id)})
        for out in (tool, shared):
            self.assertEqual(
                self.closed_days(out["buckets"], "bucket_start"), [(DAY.isoformat(), 1)]
            )

    def test_a_morning_close_east_of_utc(self):
        self.check("Asia/Tokyo", 8)

    def test_an_evening_close_west_of_utc(self):
        self.check("America/Los_Angeles", 20)

    def test_a_week_starts_on_the_members_monday(self):
        monday = date(2026, 6, 15)
        self.close_at("Asia/Tokyo", monday, 8)  # still Sunday the 14th in UTC
        page = self.run_job(
            "ai_dashboard", kind="project", project_id=self.project.project_id, period="quarter"
        )["dashboard"]
        self.assertEqual(self.closed_days(page["series"]), [(monday.isoformat(), 1)])

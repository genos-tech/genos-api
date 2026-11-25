"""Cost guard of the AI dashboards (MIP-2359).

Nothing here calls a model: `llm._client` is the scripted stub of `test_report_ai_sentences`.
Pinned, each by a test that fails when its guard is removed:

  * ZERO CREDITS: the dashboard still runs, plain, with `ai_skipped=no_credits`, and no call;
  * UNCHANGED DATA: a second run on the same numbers (even on another day) makes no call; changed
    numbers make one;
  * BOUNDED TOKENS: the answer cap and the prompt size are fixed, whatever the page lists;
  * THE CAP: at most `MAX_DASHBOARD_SCHEDULES` per member; a run at most once a day;
  * ESTIMATE ROWS and the hourly HTML-note save limit sentence are in the catalog;
  * the cost dashboard shows the `schedule` surface and the `report_dashboard` purpose.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache

from origin.models.common.schedule_models import FREQUENCY_CHOICES
from origin.search_engine import cost_dashboard
from origin.search_engine.models import AiSpendEvent
from origin.services import schedules
from origin.services.report_ai import llm, narrative
from origin.services.schedule_jobs import JOBS
from origin.services.schedule_jobs.ai_dashboard import MAX_DASHBOARD_SCHEDULES, data_fingerprint
from origin.services.schedule_text import job_catalog
from origin.tests.test_report_ai_sentences import AiBase

TIER_PATH = "origin.search_engine.quota.get_effective_tier"
TEXT = "Work is moving: two tasks closed and one is still open."


class DashboardAiBase(AiBase):
    def setUp(self):
        super().setUp()
        # The operator switch (MIP-2355) is off by default; these tests are about what follows it.
        patcher = mock.patch.dict("os.environ", {"REPORT_AI_DASHBOARDS": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model.reply = json.dumps({"n": TEXT})
        self.model.spend_tokens = 2000
        self.make_task("Open one")
        done = self.make_task("Done one", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))

    def run_dash(self, **params):
        params.setdefault("kind", "project")
        params.setdefault("project_id", self.project.project_id)
        return self.run_job("ai_dashboard", **params)


class TestZeroCredits(DashboardAiBase):
    def test_the_dashboard_runs_plain_and_says_why(self):
        with mock.patch.object(llm, "credit_block", return_value=llm.SKIP_NO_CREDITS):
            report = self.run_dash(ai_sentences=True)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(report["ai_skipped"], "no_credits")
        self.assertNotIn("narrative", report["dashboard"])
        self.assertTrue(report["dashboard"]["stats"], "the page still has its numbers")
        self.assertIn("no AI credits left", report["summary"])

    def test_the_plain_dashboard_never_asks_for_a_call(self):
        report = self.run_dash()
        self.assertEqual(self.model.calls, [])
        self.assertNotIn("ai_skipped", report)


class TestUnchangedData(DashboardAiBase):
    def test_a_second_run_on_the_same_numbers_makes_no_call(self):
        first = self.run_dash(ai_sentences=True)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(first["dashboard"]["narrative"], TEXT)
        second = self.run_dash(ai_sentences=True)
        self.assertEqual(len(self.model.calls), 1, "unchanged data is not paid for twice")
        self.assertEqual(second["dashboard"]["narrative"], TEXT)
        self.assertNotIn("ai_request_id", second)

    def test_changed_numbers_make_one_more_call(self):
        self.run_dash(ai_sentences=True)
        self.make_task("A new one")
        self.run_dash(ai_sentences=True)
        self.assertEqual(len(self.model.calls), 2)

    def test_the_date_line_is_not_part_of_the_fingerprint(self):
        page = {"title": "T", "kind": "mine", "stats": [{"label": "closed", "value": "1"}]}
        self.assertEqual(
            data_fingerprint({**page, "subtitle": "Last 30 days, as of 2026-11-25"}),  # type: ignore[typeddict-item]
            data_fingerprint({**page, "subtitle": "Last 30 days, as of 2026-11-26"}),  # type: ignore[typeddict-item]
        )
        self.assertNotEqual(
            data_fingerprint(page),  # type: ignore[arg-type]
            data_fingerprint({**page, "stats": [{"label": "closed", "value": "2"}]}),  # type: ignore[arg-type]
        )

    def test_the_same_numbers_on_another_day_reuse_the_summary(self):
        report = {
            "title": "T",
            "summary": "s",
            "sections": [{"heading": "Numbers", "items": [{"text": "1 closed"}]}],
        }
        ctx = self.ctx
        kw = {"effort": None, "default_effort": "low", "fingerprint": "abc"}
        first = narrative.narrative_for(ctx, report, today_iso="2026-11-25", **kw)
        later = narrative.narrative_for(ctx, report, today_iso="2026-11-26", **kw)
        self.assertTrue(first.called)
        self.assertFalse(later.called)
        self.assertEqual(later.text, first.text)
        self.assertEqual(len(self.model.calls), 1)

    def test_another_member_does_not_read_my_summary(self):
        self.run_dash(ai_sentences=True)
        cache.clear()  # the key holds the member: proven by the key itself
        self.assertNotEqual(
            narrative._key("u1", "low", "m", "fp"), narrative._key("u2", "low", "m", "fp")
        )


class TestBoundedTokens(DashboardAiBase):
    def test_the_call_is_capped_whatever_the_page_lists(self):
        for n in range(30):
            self.make_task(f"Closed {n}", status="Closed")
        self.run_dash(ai_sentences=True)
        (call,) = self.model.calls
        self.assertLessEqual(len(call["user"]), narrative.MAX_PROMPT_CHARS + 200)
        self.assertLessEqual(
            call["params"].max_output_tokens,
            narrative._output_cap(call["params"].thinking_budget),
        )
        self.assertEqual(call["tools"], [])

    def test_the_default_level_is_the_cheapest_rung(self):
        self.assertEqual(JOBS["ai_dashboard"].effort_default, "low")


class TestTheCap(DashboardAiBase):
    def save(self, n, **kw):
        return schedules.create(
            self.ctx,
            name=f"dash {n}",
            job="ai_dashboard",
            params={"kind": "mine", "period": "week", **kw},
            frequency="weekly",
            weekdays=[n % 7],
            time_of_day=f"{8 + n // 7:02d}:00",
        )

    def test_the_eleventh_dashboard_is_refused_in_words(self):
        with mock.patch(TIER_PATH, return_value="max"):
            made = [self.save(n, ai_sentences=bool(n % 2)) for n in range(MAX_DASHBOARD_SCHEDULES)]
            with self.assertRaisesRegex(schedules.ScheduleError, "10 dashboard schedules"):
                self.save(MAX_DASHBOARD_SCHEDULES)
            # An edit of one that exists is not a new one.
            schedules.update(self.ctx, made[0].pk, params={"kind": "mine", "period": "month"})
            # Deleting one makes room.
            schedules.delete(self.ctx, made[1].pk)
            self.save(MAX_DASHBOARD_SCHEDULES)

    def test_other_jobs_and_other_members_do_not_count(self):
        with mock.patch(TIER_PATH, return_value="max"):
            for n in range(MAX_DASHBOARD_SCHEDULES - 1):
                self.save(n)
            schedules.create(
                self.ctx,
                name="left",
                job="what_is_left",
                params={},
                frequency="daily",
                time_of_day="07:00",
            )
            self.save(MAX_DASHBOARD_SCHEDULES - 1)

    def test_no_recurrence_runs_more_than_once_a_day(self):
        self.assertEqual(
            {key for key, _ in FREQUENCY_CHOICES},
            {"daily", "weekdays", "weekly", "monthly", "once"},
        )
        zone = schedules.user_zone(self.user)
        every_day = schedules.clean_recurrence(
            frequency="weekly",
            time_of_day="09:00",
            weekdays=list(range(7)),
            month_day=None,
            run_date=None,
        )
        after = self.now
        stamps = []
        for _ in range(5):
            after = schedules.next_run_after(every_day, after, zone)
            stamps.append(after)
        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        self.assertTrue(all(g >= timedelta(hours=23) for g in gaps), gaps)


class TestCatalog(DashboardAiBase):
    def test_the_dashboard_has_estimate_rows_and_the_save_limit_sentence(self):
        row = next(r for r in job_catalog() if r["job"] == "ai_dashboard")
        levels = row["effort_cost"]["levels"]
        self.assertTrue(row["effort_cost"]["estimate"])
        self.assertTrue(levels)
        for level in levels.values():
            self.assertEqual(len(level["credits_milli"]), 2)
        self.assertIn("30", row["saves_note"])
        self.assertIn("60", row["saves_note"])
        self.assertIn("per hour", row["saves_note"])

    def test_the_schedules_page_gets_it_as_savesNote(self):
        from origin.views.common.schedule_views import _job  # noqa: PLC0415

        jobs = {r["job"]: _job(r) for r in job_catalog()}
        self.assertIn("per hour", jobs["ai_dashboard"]["savesNote"])
        self.assertNotIn("savesNote", jobs["what_is_left"])

    def test_only_a_note_saving_job_carries_it(self):
        for row in job_catalog():
            self.assertEqual("saves_note" in row, JOBS[row["job"]].delivers_note, row["job"])


class TestTheCostDashboard(DashboardAiBase):
    def test_the_spend_is_the_schedule_surface_and_the_dashboard_purpose(self):
        self.run_dash(ai_sentences=True)
        event = AiSpendEvent.objects.get()
        self.assertEqual((event.surface, event.purpose), ("schedule", "report_dashboard"))
        # The job tests freeze the clock in the past; the dashboard reads the ledger "up to now".
        with mock.patch.object(
            cost_dashboard.timezone, "now", return_value=event.created_at + timedelta(hours=1)
        ):
            data = cost_dashboard.collect(days=1)
        self.assertIn("report_dashboard", {r["key"] for r in data["purposes"]})
        self.assertIn("schedule", {r["key"] for r in data["surfaces"]})

    def test_the_other_templates_keep_their_own_purpose(self):
        self.assertNotEqual(llm.DASHBOARD_PURPOSE, llm.NARRATIVE_PURPOSE)

"""`ai_dashboard` (MIP-2353): one living HTML note per dashboard schedule.

Pinned: the page is rendered by the server with everything escaped and no scan warning; the first
run makes ONE note and every later run replaces its page; a run the tick repeats saves nothing
twice; a deleted note pauses the run with a reason; the plan decides (Free cannot save the
schedule, a reached limit fails the run in the limit's own words); the plain variant calls no
model.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core.cache import cache

from origin.models.common.schedule_models import PAUSE_NOTE, MemberSchedule
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import dashboard_page, html_notes, schedules
from origin.services.collab_push import PushOutcome
from origin.services.schedule_jobs import JOBS
from origin.tests.test_schedule_jobs import JobBase

TIER_PATH = "origin.search_engine.quota.get_effective_tier"
PUSH = "origin.services.html_notes.push_note_body_now"


def _dashboard(**extra):
    out = {
        "title": "Project dashboard: <b>X</b>",
        "kind": "project",
        "subtitle": "Last 30 days, as of 2026-11-25",
        "stats": [{"label": "tasks", "value": "3"}, {"label": "overdue", "value": "0"}],
        "breakdown": [{"label": "Open", "count": 2}, {"label": "Closed", "count": 1}],
        "series": [
            {"label": "2026-11-20", "closed": 1, "created": 2},
            {"label": "2026-11-21", "closed": 0, "created": 1},
        ],
        "lists": [{"heading": "Recently closed", "rows": ["T-1 · <script>alert(1)</script>"]}],
        "narrative": "Things went <i>well</i>.",
        "focus": "ship it & relax",
    }
    out.update(extra)
    return out


class TestThePage(JobBase):
    def test_everything_a_person_wrote_is_escaped_and_the_scan_finds_nothing(self):
        html, title, summary = dashboard_page.render(_dashboard())
        self.assertNotIn("<script", html.replace("<style>", ""))
        self.assertNotIn("<b>X</b>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;i&gt;well&lt;/i&gt;", html)
        self.assertIn("ship it &amp; relax", html)
        self.assertEqual(html_notes.scan_html(html).warnings, [])
        self.assertNotIn("<", summary)
        self.assertTrue(title.startswith("Project dashboard"))

    def test_an_empty_dashboard_still_renders_without_warnings(self):
        html, _title, _summary = dashboard_page.render(
            _dashboard(breakdown=[], series=[], lists=[], narrative="", focus="", stats=[])
        )
        self.assertEqual(html_notes.scan_html(html).warnings, [])


class TestTheJob(JobBase):
    def test_it_is_registered_and_the_ai_is_an_opt_in(self):
        job = JOBS["ai_dashboard"]
        self.assertTrue(job.delivers_note)
        self.assertFalse(job.ai_applies({"kind": "mine"}))
        self.assertTrue(job.ai_applies({"kind": "mine", "ai_sentences": True}))

    def test_a_project_dashboard_counts_the_work_and_calls_no_model(self):
        self.make_task("Open one")
        done = self.make_task("Done one", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        with mock.patch("origin.services.report_ai.narrative.narrative_for") as model:
            report = self.run_job(
                "ai_dashboard", kind="project", project_id=self.project.project_id
            )
        model.assert_not_called()
        dash = report["dashboard"]
        self.assertEqual(dash["kind"], "project")
        self.assertEqual({s["label"] for s in dash["stats"]} >= {"tasks", "overdue"}, True)
        self.assertTrue(any(c["label"] == "Closed" and c["count"] >= 1 for c in dash["breakdown"]))
        self.assertTrue(any(p["closed"] for p in dash["series"]))
        self.assertIn("Done one", str(dash["lists"]))
        self.assertNotIn("narrative", dash)

    def test_my_progress_reads_the_members_own_closes(self):
        done = self.make_task("Mine done", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        report = self.run_job("ai_dashboard", kind="mine", period="week")
        stats = {s["label"]: s["value"] for s in report["dashboard"]["stats"]}
        self.assertEqual(stats["closed"], "1")

    def test_a_project_the_member_cannot_open_fails_in_words(self):
        with self.assertRaises(schedules.ScheduleRunError):
            self.run_job("ai_dashboard", kind="project", project_id=999999)

    def test_the_parameters_must_fit_the_kind(self):
        def save(**params):
            return schedules.create(
                self.ctx,
                name=f"d {sorted(params.items())}",
                job="ai_dashboard",
                params=params,
                frequency="weekly",
                weekdays=[0],
                time_of_day="09:00",
            )

        with mock.patch(TIER_PATH, return_value="core"):
            for params in (
                {},
                {"kind": "milestone"},
                {"kind": "project"},
                {"kind": "mine", "project_id": 1},
                {"kind": "project", "project_id": 1, "milestone_id": 1},
                {"kind": "year"},
                {"kind": "mine", "period": "decade"},
            ):
                with self.assertRaises(schedules.ScheduleError, msg=str(params)):
                    save(**params)
            save(kind="mine", period="quarter")

    def test_a_free_plan_cannot_save_the_schedule(self):
        with mock.patch(TIER_PATH, return_value="free"), self.assertRaises(schedules.ScheduleError):
            schedules.create(
                self.ctx,
                name="free",
                job="ai_dashboard",
                params={"kind": "mine"},
                frequency="daily",
                time_of_day="09:00",
            )


class TestTheLivingNote(JobBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for patcher in (
            mock.patch(TIER_PATH, return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sched = schedules.create(
            self.ctx,
            name="My dashboard",
            job="ai_dashboard",
            params={"kind": "mine", "period": "month"},
            frequency="daily",
            time_of_day="09:00",
        )

    def reload(self):
        return MemberSchedule.objects.get(pk=self.sched.pk)

    def notes(self):
        return PersonalNoteMaster.objects.filter(body_format="html", owner=self.user)

    def test_the_first_run_makes_one_note_and_the_next_replaces_its_page(self):
        status, error = schedules._execute(self.reload())
        self.assertEqual((status, error), ("ok", ""))
        first = self.reload().output
        self.assertEqual(first["target"], "note")
        self.assertEqual(self.notes().count(), 1)

        status, _ = schedules._execute(self.reload())
        self.assertEqual(status, "ok")
        self.assertEqual(self.reload().output["note_id"], first["note_id"])
        self.assertEqual(self.notes().count(), 1)
        self.assertEqual(
            html_notes.latest_revision(html_notes.NOTE_TYPE_PERSONAL, first["note_id"]).rev, 2
        )

    def test_a_run_the_tick_repeats_saves_once(self):
        run_at = self.now
        self.assertEqual(schedules._execute(self.reload(), run_at=run_at)[0], "ok")
        note_id = self.reload().output["note_id"]
        self.assertEqual(schedules._execute(self.reload(), run_at=run_at)[0], "ok")
        self.assertEqual(html_notes.latest_revision(html_notes.NOTE_TYPE_PERSONAL, note_id).rev, 1)
        self.assertEqual(self.notes().count(), 1)
        # The next scheduled run is a new key: a replace, still one note.
        self.assertEqual(
            schedules._execute(self.reload(), run_at=run_at + timedelta(days=1))[0], "ok"
        )
        self.assertEqual(html_notes.latest_revision(html_notes.NOTE_TYPE_PERSONAL, note_id).rev, 2)
        self.assertEqual(self.notes().count(), 1)

    def test_a_deleted_note_pauses_the_run_with_a_reason_and_makes_no_second_note(self):
        schedules._execute(self.reload())
        PersonalNoteMaster.objects.filter(note_id=self.reload().output["note_id"]).delete()
        row = self.reload()
        status, error = schedules._execute(row)
        self.assertEqual(status, "failed")
        self.assertIn("deleted", error)
        self.assertEqual(row._pause_reason, PAUSE_NOTE)
        self.assertEqual(self.notes().count(), 0)

    def test_resuming_after_the_note_is_gone_makes_a_new_one(self):
        schedules._execute(self.reload())
        PersonalNoteMaster.objects.filter(note_id=self.reload().output["note_id"]).delete()
        MemberSchedule.objects.filter(pk=self.sched.pk).update(
            enabled=False, paused_reason=PAUSE_NOTE, next_run_at=None
        )
        schedules.set_enabled(self.ctx, self.sched.id, True)
        self.assertEqual(self.reload().output, {})
        self.assertEqual(schedules._execute(self.reload())[0], "ok")
        self.assertEqual(self.notes().count(), 1)

    def test_a_plan_limit_fails_the_run_in_its_own_words(self):
        with mock.patch(TIER_PATH, return_value="free"):
            status, error = schedules._execute(self.reload())
        self.assertEqual(status, "failed")
        self.assertIn("Core", error)
        self.assertEqual(self.notes().count(), 0)

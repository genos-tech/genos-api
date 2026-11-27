"""A dashboard page made by a skill (MIP-2399, Ken's decisions E2 and E3 of 2026-11-25).

The dashboard job takes `source: "skill"`: one of the owner's skills reads their data, read only,
and answers with DATA that the server cleans and draws (`schedule_jobs/skill_dashboard`), never
markup. What matters, each pinned by a test that fails when its guard is removed:

  * the skill is pinned like "Run a skill" pins it, the built-in kinds may not ride along, and a
    built-in dashboard still needs its kind and stores no `source`;
  * the page can only be saved where only the owner reads it (the every-reader limit cannot apply to
    what a model made of the owner's view), at save AND before every run, and a run with another
    reader stops BEFORE the model is called;
  * the model is told the shape of the data, not the shape of a report, and only reads;
  * its loop is in no savepoint, so what it spent cannot be rolled back with a failed run;
  * a script or link in the answer never reaches the page; an answer that is not a page fails in
    words and the previous page stays; no credits means no call and the old page stays.
"""

from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase

from origin.models.common.schedule_models import STATUS_FAILED, STATUS_OK, MemberSchedule
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import app_write_tools
from origin.search_engine.llm.choice import get_llm_choice
from origin.services import dashboard_page, schedule_text, schedules
from origin.services.collab_push import PushOutcome
from origin.services.schedule_jobs import JOBS, ai_dashboard, run_skill, skill_dashboard
from origin.tests.test_schedule_job_ai_dashboard import PUSH, TIER_PATH
from origin.tests.test_schedule_run_skill import Base
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_VIEWER

PAGE = json.dumps(
    {
        "title": "My week",
        "subtitle": "Last 7 days",
        "stats": [{"label": "closed", "value": "5"}, {"label": "open", "value": "2"}],
        "breakdown": [{"label": "Closed", "count": 5}, {"label": "Open", "count": 2}],
        "series": [{"label": "2026-11-20", "closed": 2, "created": 1}],
        "lists": [{"heading": "Done", "rows": ["MIP-1 · A task", "MIP-2 · Another"]}],
    }
)


class DashBase(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        for patcher in (
            mock.patch(TIER_PATH, return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agent.answer = PAGE

    def dash(self, skill, *, name="Skill board", params=None, **kw):
        base = {"source": "skill", "skill_id": str(skill.id)}
        return schedules.create(
            self.ctx,
            name=name,
            job="ai_dashboard",
            params={**base, **(params or {})},
            frequency="daily",
            time_of_day=kw.pop("time_of_day", "08:30"),
            **kw,
        )

    def notes(self):
        return PersonalNoteMaster.objects.filter(body_format="html", owner=self.user)


class SavingTests(DashBase):
    def test_a_skill_dashboard_is_saved_with_the_published_version_pinned(self):
        skill = self.skill()
        sched = self.dash(skill, params={"input": "this week"})
        self.assertEqual(
            sched.params,
            {"source": "skill", "skill_id": str(skill.id), "version_no": 1, "input": "this week"},
        )

    def test_the_built_in_kinds_may_not_ride_along_with_a_skill(self):
        skill = self.skill()
        for key, value in (
            ("kind", "mine"),
            ("period", "week"),
            ("focus", "blockers"),
            ("project_id", 1),
            ("milestone_id", 1),
            ("ai_sentences", True),
        ):
            with self.subTest(key=key), self.assertRaises(schedules.ScheduleError) as caught:
                self.dash(skill, params={key: value})
            self.assertIn("not one made by a skill", str(caught.exception))
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_the_skill_keys_are_refused_on_a_built_in_dashboard(self):
        skill = self.skill()
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.create(
                self.ctx,
                name="Mine",
                job="ai_dashboard",
                params={"kind": "mine", "skill_id": str(skill.id)},
                frequency="daily",
                time_of_day="09:00",
            )
        self.assertIn("only for a dashboard made by a skill", str(caught.exception))

    def test_a_built_in_dashboard_still_needs_a_kind_and_stores_no_source(self):
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.create(
                self.ctx,
                name="None",
                job="ai_dashboard",
                params={},
                frequency="daily",
                time_of_day="09:00",
            )
        self.assertIn("needs `kind`", str(caught.exception))
        sched = schedules.create(
            self.ctx,
            name="Mine",
            job="ai_dashboard",
            params={"source": "template", "kind": "mine"},
            frequency="daily",
            time_of_day="09:05",
        )
        self.assertEqual(sched.params, {"kind": "mine"})

    def test_a_skill_the_owner_may_not_run_is_refused_like_a_missing_one(self):
        theirs = self.skill(self.user2, visibility="private", slug="private-one")
        draft = self.skill(visibility="private", slug="draft-one")
        for skill in (theirs, draft):
            with self.subTest(skill=skill.slug), self.assertRaises(schedules.ScheduleError):
                self.dash(skill)
        with self.assertRaises(schedules.ScheduleError) as caught:
            schedules.create(
                self.ctx,
                name="Nothing",
                job="ai_dashboard",
                params={"source": "skill"},
                frequency="daily",
                time_of_day="09:10",
            )
        self.assertIn("needs `skill_id`", str(caught.exception))

    def test_the_owner_only_sentence_fits_in_the_error_a_failed_run_keeps(self):
        self.assertLess(len(ai_dashboard.SKILL_OWNER_ONLY), schedules.MAX_ERROR_CHARS)

    def test_a_place_other_people_read_is_refused_and_my_notes_is_fine(self):
        skill = self.skill()
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Everyone",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        with self.assertRaises(schedules.ScheduleError) as caught:
            self.dash(skill, params={"save_to": "team_folder", "folder_id": folder.folder_id})
        self.assertEqual(str(caught.exception), ai_dashboard.SKILL_OWNER_ONLY)
        self.assertEqual(MemberSchedule.objects.count(), 0)
        self.assertEqual(self.dash(skill).params["source"], "skill")

    def test_the_plans_count_of_skill_schedules_does_not_apply_to_a_dashboard(self):
        skill = self.skill()
        with mock.patch.object(run_skill, "skill_schedules_limit", return_value=0):
            with self.assertRaises(schedules.ScheduleLimitReached):
                self.schedule(skill)
            self.assertEqual(self.dash(skill).job, "ai_dashboard")

    def test_a_skill_dashboard_counts_toward_the_dashboard_cap(self):
        skill = self.skill()
        with mock.patch.object(ai_dashboard, "MAX_DASHBOARD_SCHEDULES", 1):
            self.dash(skill)
            with self.assertRaises(schedules.ScheduleError):
                self.dash(skill, name="Second", time_of_day="09:20")

    def test_the_job_always_uses_the_model_and_runs_unwrapped(self):
        job = JOBS["ai_dashboard"]
        self.assertTrue(job.unwrapped)
        self.assertTrue(job.ai_applies({"source": "skill", "skill_id": "x"}))
        self.assertFalse(job.ai_applies({"kind": "mine"}))
        self.assertTrue(job.ai_applies({"kind": "mine", "ai_sentences": True}))


class RunTests(DashBase):
    def test_the_page_is_made_from_the_answer_and_replaced_on_the_next_run(self):
        sched = self.dash(self.skill())
        self.assertEqual(schedules._execute(sched), (STATUS_OK, ""))
        sched.refresh_from_db()
        note_id = sched.output["note_id"]
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note_id).exists())
        self.assertEqual(self.notes().count(), 1)
        self.agent.answer = json.dumps(
            {"title": "My week", "subtitle": "Again", "stats": [{"label": "closed", "value": "9"}]}
        )
        self.assertEqual(schedules._execute(sched), (STATUS_OK, ""))
        sched.refresh_from_db()
        self.assertEqual(sched.output["note_id"], note_id)
        self.assertEqual(self.notes().count(), 1)

    def test_the_model_is_told_the_shape_of_the_data_and_not_of_a_report(self):
        sched = self.dash(self.skill())
        schedules._execute(sched)
        told = self.system_extra()
        self.assertIn(skill_dashboard.DIRECTIVE, told)
        self.assertIn(run_skill.UNATTENDED_RULES, told)
        self.assertNotIn("Write the answer as a short report", told)

    def test_the_run_uses_the_effort_the_schedule_reports(self):
        # MIP-2423: with no level stored the form shows the job's default, so the run takes it too.
        used = []

        def agent(query, ctx, emit, **kwargs):
            used.append(get_llm_choice().effort)
            return self.agent(query, ctx, emit, **kwargs)

        sched = self.dash(self.skill())
        job = JOBS["ai_dashboard"]
        with mock.patch.object(controller, "run_agent", agent):
            for stored, level in (("", "low"), ("low", "low"), ("high", "high")):
                with self.subTest(stored=stored):
                    sched.ai_effort = stored
                    sched.save()
                    self.assertEqual(schedule_text.effective_effort(sched, job), level)
                    self.assertEqual(schedules._execute(sched), (STATUS_OK, ""))
                    self.assertEqual(used[-1], level)

    def test_the_run_only_reads_and_the_loop_is_in_no_savepoint(self):
        sched = self.dash(self.skill())
        outside = len(connection.savepoint_ids)
        self.assertEqual(schedules._execute(sched)[0], STATUS_OK)
        self.assertTrue({t.name for t in app_write_tools()} <= self.disabled())
        self.assertEqual(self.agent.calls[-1]["savepoints"], outside)

    def test_markup_and_links_in_the_answer_never_reach_the_page(self):
        self.agent.answer = json.dumps(
            {
                "title": "<script>alert(1)</script>Week",
                "subtitle": '<img src=x onerror="steal()">',
                "stats": [{"label": "<b>closed</b>", "value": "5"}],
                "lists": [
                    {
                        "heading": "Links",
                        "rows": ["[click](https://evil.example/x?d=secret)", "<a href=x>go</a>"],
                    }
                ],
                "html": "<script>steal()</script>",
            }
        )
        report = ai_dashboard.run(self.ctx, {"source": "skill", "skill_id": str(self.skill().id)})
        html, _title, _summary = dashboard_page.render(report["dashboard"])
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<a href", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("steal()</script>", html)

    def test_an_answer_that_is_not_a_page_fails_in_words_and_the_page_stays(self):
        sched = self.dash(self.skill())
        schedules._execute(sched)
        sched.refresh_from_db()
        note_id = sched.output["note_id"]
        for bad in ("Here is your week: it went well.", "{}", '{"stats": []}', "[1, 2]"):
            self.agent.answer = bad
            with self.subTest(answer=bad):
                status, error = schedules._execute(sched)
                self.assertEqual(status, STATUS_FAILED)
                self.assertIn("The page was not changed", error)
        sched.refresh_from_db()
        self.assertEqual(sched.output["note_id"], note_id)
        self.assertEqual(self.notes().count(), 1)

    def test_a_failed_model_run_fails_in_words_and_the_page_stays(self):
        sched = self.dash(self.skill())
        schedules._execute(sched)
        self.agent.error = "boom"
        status, error = schedules._execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(error, run_skill.FAILED_RUN)
        self.assertEqual(self.notes().count(), 1)

    def test_at_zero_credits_nothing_is_called_and_the_old_page_stays(self):
        sched = self.dash(self.skill())
        schedules._execute(sched)
        calls = len(self.agent.calls)
        with mock.patch.object(run_skill, "_skip_code", return_value="no_credits"):
            status, error = schedules._execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        self.assertEqual(len(self.agent.calls), calls)
        self.assertEqual(self.notes().count(), 1)

    def test_a_reader_added_later_stops_the_run_before_the_model_is_called(self):
        sched = self.dash(self.skill())
        schedules._execute(sched)
        sched.refresh_from_db()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=sched.output["note_id"],
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        calls = len(self.agent.calls)
        status, error = schedules._execute(sched)
        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(error, ai_dashboard.SKILL_OWNER_ONLY)
        self.assertEqual(len(self.agent.calls), calls)
        self.assertNotIn(self.user2.username, error)


class RunSkillIsUnchangedTests(DashBase):
    def test_the_run_a_skill_job_is_still_told_the_report_shape(self):
        sched = self.schedule(self.skill())
        self.assertEqual(self.execute(sched)[0], STATUS_OK)
        self.assertTrue(self.system_extra().endswith(run_skill.UNATTENDED))

    def test_the_rules_are_the_front_of_the_unattended_text(self):
        self.assertTrue(run_skill.UNATTENDED.startswith(run_skill.UNATTENDED_RULES))
        self.assertNotIn("short report", run_skill.UNATTENDED_RULES)
        self.assertIn("SCHEDULED RUN", run_skill.UNATTENDED_RULES)


class ParserTests(SimpleTestCase):
    def parse(self, answer, **kw):
        return skill_dashboard.parse_dashboard(answer, default_title=kw.pop("title", "Skill"))

    def test_a_plain_object_a_fenced_one_and_one_with_words_round_it_all_parse(self):
        for answer in (PAGE, f"```json\n{PAGE}\n```", f"Here you go:\n{PAGE}\nEnjoy."):
            with self.subTest(answer=answer[:12]):
                dash = self.parse(answer)
                self.assertEqual(dash["title"], "My week")
                self.assertEqual(dash["kind"], "skill")
                self.assertEqual(len(dash["stats"]), 2)

    def test_each_part_is_cut_to_what_the_page_draws(self):
        big = {
            "title": "T" * 500,
            "stats": [{"label": f"l{i}", "value": str(i)} for i in range(30)],
            "breakdown": [{"label": f"b{i}", "count": i} for i in range(40)],
            "series": [{"label": f"d{i}", "closed": i, "created": i} for i in range(100)],
            "lists": [
                {"heading": f"h{i}", "rows": [f"r{j}" for j in range(40)]} for i in range(20)
            ],
            "narrative": "n" * 5000,
            "note": "x" * 5000,
        }
        dash = self.parse(json.dumps(big))
        self.assertEqual(len(dash["title"]), 120)
        self.assertEqual(len(dash["stats"]), skill_dashboard.MAX_STATS)
        self.assertEqual(len(dash["breakdown"]), skill_dashboard.MAX_BREAKDOWN)
        self.assertEqual(len(dash["series"]), skill_dashboard.MAX_SERIES)
        self.assertEqual(len(dash["lists"]), skill_dashboard.MAX_LISTS)
        self.assertTrue(all(len(b["rows"]) == skill_dashboard.MAX_ROWS for b in dash["lists"]))
        self.assertLessEqual(len(dash["narrative"]), skill_dashboard.MAX_NARRATIVE)
        self.assertLessEqual(len(dash["note"]), 300)

    def test_the_title_falls_back_to_the_skills_and_unknown_keys_are_ignored(self):
        dash = self.parse(
            json.dumps({"stats": [{"label": "a", "value": "1"}], "href": "x"}), title="Weekly"
        )
        self.assertEqual(dash["title"], "Weekly")
        self.assertNotIn("href", dash)

    def test_control_and_invisible_characters_are_removed(self):
        dash = self.parse(
            json.dumps({"title": "A​B\x00C‮", "stats": [{"label": "x\x07y", "value": "1"}]})
        )
        self.assertEqual(dash["title"], "ABC")
        self.assertEqual(dash["stats"][0]["label"], "xy")

    def test_a_count_must_be_a_whole_non_negative_number(self):
        dash = self.parse(
            json.dumps(
                {
                    "stats": [{"label": "a", "value": "1"}],
                    "breakdown": [
                        {"label": "ok", "count": 3},
                        {"label": "str", "count": "4"},
                        {"label": "neg", "count": -1},
                        {"label": "bool", "count": True},
                        {"label": "float", "count": 2.5},
                        {"label": "huge", "count": 10**12},
                    ],
                }
            )
        )
        self.assertEqual([b["label"] for b in dash["breakdown"]], ["ok", "str"])

    def test_an_answer_with_nothing_to_draw_or_no_data_is_refused_in_words(self):
        for bad in ("", "no data here", "{}", '{"title": "x"}', "[1]", "{not json}", '{"a": 1}'):
            with self.subTest(answer=bad), self.assertRaises(skill_dashboard.DashboardAnswerError):
                self.parse(bad)

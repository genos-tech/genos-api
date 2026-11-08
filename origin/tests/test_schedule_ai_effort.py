"""The AI effort of a schedule: low / medium / high (MIP-1212).

What is pinned, each by a test that fails when its guard is removed:

  * a schedule carries an effort, validated when it is SAVED (REST and the connected-app tools),
    empty = the job's own default; a job that uses no AI ignores it and says so in the catalog
    (`usesAi`, `effortDefault` null, `effortCost` null);
  * the level changes the model rung and the thinking budget of THAT schedule's AI call, and the
    spend rows carry it; another schedule is not touched;
  * an invalid or retired level never fails a run: it is the job's default, and a level above the
    member's plan is held to the plan's ceiling;
  * the catalog tells the page what a run costs at each level, marked an estimate;
  * a member cannot send the run-time key that carries the level (it is not a parameter).
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.core.cache import cache

from origin.models.common.schedule_models import MemberSchedule
from origin.search_engine.models import AiSpendEvent
from origin.services import account_export, schedule_text, schedules
from origin.services.report_ai import effort as effort_mod
from origin.services.schedule_jobs import AI_EFFORT_KEY, JOBS
from origin.services.user_time import user_zone
from origin.tests.test_report_ai_sentences import AiBase, answer

LIST = "/api/v2/schedules/"
AI_JOB = "what_to_do_today"
PLAIN_JOB = "what_you_did"


class Base(AiBase):
    def make(self, job=AI_JOB, **kw):
        return schedules.create(
            self.ctx,
            name=kw.pop("name", "Plan"),
            job=job,
            frequency="daily",
            time_of_day="09:00",
            **kw,
        )

    def execute(self, sched):
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            return schedules._execute(sched)

    def prime(self):
        """A task whose comment gives the model something to be asked about."""
        self.overdue(comment="Waiting for the vendor.")
        self.model.reply = answer(t1="The vendor has not answered.")
        self.model.spend_tokens = 500


class TestChoosing(Base):
    def test_it_is_empty_by_default_and_the_job_default_applies(self):
        sched = self.make()
        self.assertEqual(sched.ai_effort, "")
        row = schedule_text.present(sched, user_zone(self.user))
        self.assertEqual((row["ai_effort"], row["ai_effort_effective"]), (None, "low"))

    def test_create_and_update_take_a_level_and_an_empty_one_resets_it(self):
        sched = self.make(ai_effort="medium")
        self.assertEqual(sched.ai_effort, "medium")
        sched = schedules.update(self.ctx, sched.id, ai_effort="high")
        self.assertEqual(MemberSchedule.objects.get(pk=sched.pk).ai_effort, "high")
        for reset in ("", None):
            schedules.update(self.ctx, sched.id, ai_effort="high")
            schedules.update(self.ctx, sched.id, ai_effort=reset)
            self.assertEqual(MemberSchedule.objects.get(pk=sched.pk).ai_effort, "")

    def test_a_level_the_catalog_does_not_have_is_refused_when_saved(self):
        for bad in ("ultra", "Low", " low", "xhigh", 3, ["low"], "medium "):
            with self.assertRaises(schedules.ScheduleError, msg=repr(bad)):
                self.make(name=f"x{bad!r}", ai_effort=bad)
        sched = self.make()
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.id, ai_effort="max")
        self.assertEqual(MemberSchedule.objects.get(pk=sched.pk).ai_effort, "")

    def test_the_run_time_key_is_not_a_parameter_a_member_can_send(self):
        with self.assertRaises(schedules.ScheduleError):
            self.make(params={AI_EFFORT_KEY: "high"})

    def test_a_job_that_uses_no_ai_takes_the_value_and_shows_none(self):
        sched = self.make(job=PLAIN_JOB, ai_effort="high")
        row = schedule_text.present(sched, user_zone(self.user))
        self.assertNotIn("ai_effort", row)
        self.assertNotIn("ai_effort_effective", row)

    def test_the_effective_level_is_held_to_the_plan(self):
        sched = self.make(ai_effort="high")
        with mock.patch.object(effort_mod, "_ceiling", return_value="medium"):
            row = schedule_text.present(sched, user_zone(self.user))
        self.assertEqual((row["ai_effort"], row["ai_effort_effective"]), ("high", "medium"))

    def test_the_export_carries_it(self):
        self.make(ai_effort="medium")
        data = account_export.build_export(self.user)
        self.assertEqual([s["ai_effort"] for s in data["schedules"]], ["medium"])


class TestTheCatalog(Base):
    def entry(self, job):
        return next(j for j in schedule_text.job_catalog() if j["job"] == job)

    def test_an_ai_job_says_so_and_its_default_and_cost_per_level(self):
        row = self.entry(AI_JOB)
        self.assertIs(row["uses_ai"], True)
        self.assertEqual(row["effort_default"], JOBS[AI_JOB].effort_default)
        cost = row["effort_cost"]
        self.assertIs(cost["estimate"], True)
        self.assertEqual(set(cost["levels"]), {"low", "medium", "high"})
        for level, entry in cost["levels"].items():
            self.assertEqual(entry["model"], settings.LLM_CATALOG.model_for_effort("gemini", level))
            self.assertLess(entry["usd_micro"][0], entry["usd_micro"][1])
        lows = [cost["levels"][lv]["usd_micro"][1] for lv in ("low", "medium", "high")]
        self.assertEqual(lows, sorted(lows))

    def test_a_job_with_no_ai_says_so_and_has_no_default_or_cost(self):
        for job in ("what_you_did", "what_is_left", "milestone_updates"):
            row = self.entry(job)
            self.assertEqual(
                (row["uses_ai"], row["effort_default"], row["effort_cost"]), (False, None, None)
            )

    def test_the_job_defaults_are_levels_the_catalog_has(self):
        for job in JOBS.values():
            self.assertIn(job.effort_default, effort_mod.EFFORTS, job.key)

    def test_the_cost_follows_the_catalog_prices(self):
        base = self.entry(AI_JOB)["effort_cost"]["levels"]["medium"]["usd_micro"]
        price = settings.LLM_CATALOG.price_for("gemini-3.6-flash")
        doubled = type(price)(
            **{**price.__dict__, "input": price.input * 2, "output": price.output * 2}
        )
        with mock.patch.object(type(settings.LLM_CATALOG), "price_for", return_value=doubled):
            again = self.entry(AI_JOB)["effort_cost"]["levels"]["medium"]["usd_micro"]
        self.assertGreater(again[1], base[1])


class TestOverRest(Base):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.team_id = str(self.team.team_id)

    def body(self, **over):
        return {
            "team_id": self.team_id,
            "name": "Morning",
            "job": AI_JOB,
            "frequency": "daily",
            "timeOfDay": "09:00",
            **over,
        }

    def test_create_with_a_level_and_the_row_carries_it(self):
        res = self.client.post(LIST, self.body(aiEffort="medium"), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        row = res.data["schedule"]
        self.assertEqual((row["aiEffort"], row["aiEffortEffective"]), ("medium", "medium"))

    def test_create_without_one_has_none_and_the_default_in_effect(self):
        row = self.client.post(LIST, self.body(), format="json").data["schedule"]
        self.assertEqual((row["aiEffort"], row["aiEffortEffective"]), (None, "low"))

    def test_patch_changes_and_null_resets_it(self):
        sid = self.client.post(LIST, self.body(), format="json").data["schedule"]["id"]
        url = f"{LIST}{sid}/?team_id={self.team_id}"
        row = self.client.patch(url, {"aiEffort": "high"}, format="json").data["schedule"]
        self.assertEqual(row["aiEffort"], "high")
        row = self.client.patch(url, {"aiEffort": None}, format="json").data["schedule"]
        self.assertIsNone(row["aiEffort"])

    def test_an_invalid_level_is_a_400_with_a_sentence(self):
        res = self.client.post(LIST, self.body(aiEffort="ultra"), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("low, medium or high", res.data["error"])
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_plain_job_row_has_no_effort_fields(self):
        row = self.client.post(LIST, self.body(job=PLAIN_JOB, aiEffort="high"), format="json").data[
            "schedule"
        ]
        self.assertNotIn("aiEffort", row)
        self.assertNotIn("aiEffortEffective", row)

    def test_the_list_has_the_catalog_the_page_hides_the_control_by(self):
        res = self.client.get(f"{LIST}?team_id={self.team_id}")
        jobs = {j["job"]: j for j in res.data["jobs"]}
        ai = jobs[AI_JOB]
        self.assertIs(ai["usesAi"], True)
        self.assertEqual(ai["effortDefault"], JOBS[AI_JOB].effort_default)
        self.assertIs(ai["effortCost"]["estimate"], True)
        self.assertEqual(set(ai["effortCost"]["levels"]), {"low", "medium", "high"})
        low = ai["effortCost"]["levels"]["low"]
        self.assertEqual(set(low), {"model", "creditsMilli", "usdMicro"})
        for job in (PLAIN_JOB, "what_is_left"):
            self.assertEqual(
                (jobs[job]["usesAi"], jobs[job]["effortDefault"], jobs[job]["effortCost"]),
                (False, None, None),
            )
        self.assertEqual(
            set(ai),
            {"job", "title", "description", "params", "usesAi", "effortDefault", "effortCost"},
        )


class TestOverMcp(Base):
    def test_create_and_update_take_the_level_and_list_shows_the_catalog(self):
        from origin.views.public.mcp.secretary.create_my_schedule import CREATE_MY_SCHEDULE
        from origin.views.public.mcp.secretary.list_my_schedules import LIST_MY_SCHEDULES
        from origin.views.public.mcp.secretary.update_my_schedule import UPDATE_MY_SCHEDULE

        out = CREATE_MY_SCHEDULE.run(
            {
                "name": "Plan",
                "job": AI_JOB,
                "frequency": "daily",
                "time_of_day": "09:00",
                "ai_effort": "medium",
            },
            self.ctx,
        )
        sid = out["schedule"]["schedule_id"]
        self.assertEqual(out["schedule"]["ai_effort"], "medium")
        out = UPDATE_MY_SCHEDULE.run({"schedule_id": sid, "ai_effort": "high"}, self.ctx)
        self.assertEqual(out["schedule"]["ai_effort"], "high")
        listed = LIST_MY_SCHEDULES.run({}, self.ctx)
        ai = next(j for j in listed["jobs"] if j["job"] == AI_JOB)
        self.assertTrue(ai["uses_ai"])
        self.assertIs(ai["effort_cost"]["estimate"], True)

    def test_an_invalid_level_is_a_tool_error_and_the_enum_is_the_catalogs(self):
        from origin.search_engine.agent.tools import ToolError
        from origin.views.public.mcp.secretary.create_my_schedule import CREATE_MY_SCHEDULE

        with self.assertRaises(ToolError):
            CREATE_MY_SCHEDULE.run(
                {
                    "name": "P",
                    "job": AI_JOB,
                    "frequency": "daily",
                    "time_of_day": "09:00",
                    "ai_effort": "max",
                },
                self.ctx,
            )
        enum = CREATE_MY_SCHEDULE.parameters_schema["properties"]["ai_effort"]["enum"]
        self.assertEqual(enum, list(effort_mod.EFFORTS))


class TestTheRun(Base):
    def test_each_level_runs_its_own_model_and_the_rows_carry_it(self):
        seen = {}
        for n, level in enumerate(("low", "medium", "high")):
            cache.clear()
            self.model.calls.clear()
            AiSpendEvent.objects.all().delete()
            self.prime() if level == "low" else None
            sched = self.make(name=f"S {level}", ai_effort=level, params={"changed_hours": 20 + n})
            self.assertEqual(self.execute(sched), ("ok", ""))
            call = self.model.calls[-1]
            row = AiSpendEvent.objects.get()
            seen[level] = (call["model"], call["params"].thinking_budget, row.effort)
        for level, (model, budget, effort) in seen.items():
            self.assertEqual(model, settings.LLM_CATALOG.model_for_effort("gemini", level))
            self.assertEqual(budget, effort_mod.THINKING_BUDGET[level])
            self.assertEqual(effort, level)
        self.assertEqual(len({m for m, _, _ in seen.values()}), 3)

    def test_another_schedule_is_not_touched(self):
        self.prime()
        high = self.make(name="High", ai_effort="high")
        plain_default = self.make(name="Default", params={"changed_hours": 25})
        self.execute(high)
        cache.clear()
        self.execute(plain_default)
        models = [c["model"] for c in self.model.calls]
        self.assertEqual(models[0], settings.LLM_CATALOG.model_for_effort("gemini", "high"))
        self.assertEqual(
            models[1], settings.LLM_CATALOG.model_for_effort("gemini", JOBS[AI_JOB].effort_default)
        )

    def test_empty_means_the_jobs_default(self):
        self.prime()
        self.execute(self.make())
        self.assertEqual(
            self.model.calls[0]["model"],
            settings.LLM_CATALOG.model_for_effort("gemini", JOBS[AI_JOB].effort_default),
        )

    def test_a_level_retired_since_it_was_saved_falls_back_and_never_fails_the_run(self):
        self.prime()
        sched = self.make(ai_effort="medium")
        MemberSchedule.objects.filter(pk=sched.pk).update(ai_effort="retired")
        status, error = self.execute(MemberSchedule.objects.get(pk=sched.pk))
        self.assertEqual((status, error), ("ok", ""))
        self.assertEqual(
            self.model.calls[0]["model"],
            settings.LLM_CATALOG.model_for_effort("gemini", JOBS[AI_JOB].effort_default),
        )

    def test_a_level_above_the_plan_runs_at_the_plans_ceiling(self):
        self.prime()
        sched = self.make(ai_effort="high")
        with mock.patch.object(effort_mod, "_ceiling", return_value="medium"):
            self.execute(sched)
        self.assertEqual(
            self.model.calls[0]["model"], settings.LLM_CATALOG.model_for_effort("gemini", "medium")
        )

    def test_a_job_that_uses_no_ai_ignores_the_level_and_calls_no_model(self):
        sched = self.make(job=PLAIN_JOB, ai_effort="high")
        self.assertEqual(self.execute(sched), ("ok", ""))
        self.assertEqual(self.model.calls, [])

    def test_the_level_reaches_the_job_only_for_a_job_that_uses_ai(self):
        ai = self.make(ai_effort="medium")
        seen = {}

        def spy(ctx, params):
            seen.update(params)
            return {"title": "t", "summary": "s", "sections": []}

        from origin.services.schedule_jobs import Job

        with mock.patch.dict(JOBS, {"spy": Job("spy", "Spy", "d", spy)}):
            plain = schedules.create(
                self.ctx,
                name="Spy",
                job="spy",
                frequency="daily",
                time_of_day="09:00",
                ai_effort="high",
            )
            self.execute(plain)
        self.assertNotIn(AI_EFFORT_KEY, seen)
        self.assertEqual(schedules._run_params(ai, JOBS[AI_JOB])[AI_EFFORT_KEY], "medium")

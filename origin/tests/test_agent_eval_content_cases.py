"""MIP-905 eval cases (tags t905, t905-a2): the case files, the two arms, and the group-layer support
they need (`member_prefs`, `llm`, `pending_write`, `needs`). No model is called.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent import content_length
from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import case_runner, content_checks, harness

LEVELS = ("standard", "short", "long")


def pending(tool, arguments):
    return {"type": "tool_call_pending_approval", "tool_name": tool, "arguments": arguments}


def plan(*bodies, milestone="A milestone."):
    return pending(
        "create_task_plan",
        {
            "milestone": {"title": "M", "description_markdown": milestone},
            "tasks": [{"title": f"t{i}", "content_markdown": b} for i, b in enumerate(bodies)],
        },
    )


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = harness.load_cases(include_legacy=False)
        self.t905 = [c for c in self.cases if "t905" in c.tags]
        self.a2 = [c for c in self.cases if "t905-a2" in c.tags]

    def test_every_question_exists_at_every_level_in_both_languages(self):
        for lang in ("en", "ja"):
            ids = {c.id for c in self.t905 if c.lang == lang}
            for kind in ("spec-note", "plan", "chat"):
                for level in LEVELS:
                    self.assertIn(f"t905.{kind}.{level}.{lang}", ids)
            for level in ("standard", "long"):
                self.assertIn(f"t905.bigplan.{level}.{lang}", ids)

    def test_a2_cases_in_both_languages_with_a_negative(self):
        for lang in ("en", "ja"):
            ids = {c.id for c in self.a2 if c.lang == lang}
            self.assertEqual(ids, {f"t905-a2.{k}.{lang}" for k in ("plan", "note", "status-only")})

    def test_the_member_level_is_the_cases_and_standard_runs_on_arm0_too(self):
        for c in self.t905:
            level = c.id.split(".")[2]
            self.assertEqual(c.raw["member_prefs"], {"content_length": level}, c.id)
            if level == "standard":
                self.assertIsNone(c.requires_arm, f"{c.id}: Standard is the arm0 comparison")
            else:
                self.assertEqual(c.requires_arm, "t905", c.id)

    def test_every_level_of_a_question_sends_the_same_prompt(self):
        for lang in ("en", "ja"):
            for kind in ("spec-note", "plan", "chat", "bigplan"):
                queries = {
                    c.raw["query"] for c in self.t905 if c.lang == lang and f".{kind}." in c.id
                }
                self.assertEqual(
                    len(queries), 1, f"{kind}.{lang}: levels must differ only in the member"
                )

    def test_japanese_cases_are_asked_in_japanese_with_character_bounds(self):
        for c in self.t905 + self.a2:
            if c.lang == "ja":
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)

    def test_short_is_bounded_above_and_long_below_in_characters(self):
        for c in self.t905:
            level = c.id.split(".")[2]
            spec = (c.raw["expect"] or {}).get("pending_write") or {}
            if ".chat." in c.id or ".bigplan." in c.id:
                continue
            bounded_above = {k for k in spec if k.endswith("_at_most")}
            bounded_below = {k for k in spec if k.endswith("_at_least")} - {"tasks_at_least"}
            if level == "short":
                self.assertTrue(bounded_above, c.id)
            if level == "long":
                self.assertTrue(bounded_below, c.id)

    def test_chat_answers_are_bounded_at_every_level(self):
        for c in self.t905:
            if ".chat." in c.id:
                self.assertIn("answer_chars_at_most", c.raw["expect"], c.id)
                self.assertIn("tools_used_excludes", c.raw["expect"], c.id)

    def test_every_write_case_checks_for_a_cut_off_body(self):
        for c in self.t905 + self.a2:
            if ".chat." in c.id or "status-only" in c.id:
                continue
            self.assertTrue(c.raw["expect"]["pending_write"].get("not_cut_off"), c.id)

    def test_the_capped_cases_pin_gemini_low_and_say_what_they_need(self):
        for c in self.t905:
            if ".bigplan." in c.id:
                self.assertEqual(c.raw["llm"], {"provider": "gemini", "effort": "low"})
                self.assertEqual(c.raw["needs_env"], {"AGENT_EFFORT_LEVELS": True})
                self.assertGreaterEqual(c.raw["est"]["output_tokens"], 8000)
        for c in self.a2:
            self.assertEqual(c.raw["llm"], {"provider": "gemini", "effort": "medium"})
            self.assertEqual(
                c.raw["needs_env"], {"AGENT_EFFORT_LEVELS": True, "AGENT_TWO_TIER_LOOP": True}
            )

    def test_the_group_only_keys_never_reach_the_runner(self):
        raw = case_runner.prepare_case(next(c for c in self.t905 if c.id == "t905.plan.long.en"))
        for key in ("member_prefs", "llm", "needs_env", "requires_arm", "est"):
            self.assertNotIn(key, raw)
        # and the group expect keys are taken out by the case runner before the runner sees them
        for key in ("pending_write", "answer_chars_at_most", "answer_chars_at_least"):
            self.assertIn(key, harness.GROUP_EXPECT_KEYS)


class ArmTests(SimpleTestCase):
    def setUp(self):
        self.arms = harness.load_arms()

    def test_the_arms(self):
        self.assertEqual(self.arms["t905"].effective, {"AGENT_CONTENT_LENGTH": True})
        self.assertEqual(self.arms["t905-a2"].effective, {"AGENT_BODY_ON_USER_MODEL": True})
        self.assertEqual(
            self.arms["t905-a2"].needs, {"AGENT_EFFORT_LEVELS": True, "AGENT_TWO_TIER_LOOP": True}
        )
        self.assertEqual(self.arms["t905"].needs, {})

    def test_the_flags_exist_and_are_off_so_neither_arm_is_refused(self):
        for name in ("t905", "t905-a2"):
            self.assertEqual(harness.validate_overrides(self.arms[name]), [], name)

    def test_the_union_all_on_arm_holds_both(self):
        union = harness.all_on_arm(self.arms).effective
        self.assertTrue(union["AGENT_CONTENT_LENGTH"])
        self.assertTrue(union["AGENT_BODY_ON_USER_MODEL"])


class PlanTests(SimpleTestCase):
    def setUp(self):
        self.cases = [
            c for c in harness.load_cases(include_legacy=False) if {"t905", "t905-a2"} & c.tags
        ]
        self.arms = harness.load_arms()

    def plan(self, arm_names):
        return harness.build_plan(self.cases, self.arms, arm_names=arm_names)

    def test_arm0_runs_standard_and_skips_short_and_long(self):
        plan = self.plan(["t905"])
        by = {i.case.id: i for i in plan.for_arm("arm0")}
        self.assertEqual(by["t905.plan.standard.en"].skip, "")
        self.assertTrue(by["t905.plan.long.en"].skip.startswith("requires_arm"))
        self.assertTrue(by["t905.plan.short.ja"].skip.startswith("requires_arm"))

    def test_the_t905_arm_runs_every_level(self):
        runs = {i.case.id for i in self.plan(["t905"]).for_arm("t905") if not i.skip}
        self.assertEqual(len(runs), 22)

    def test_the_a2_arm_runs_only_its_cases_and_arm0_runs_them_as_the_baseline(self):
        plan = self.plan(["t905-a2"])
        self.assertEqual(
            {i.case.id for i in plan.for_arm("t905-a2")},
            {c.id for c in self.cases if "t905-a2" in c.tags},
        )
        self.assertTrue(
            {c.id for c in self.cases if "t905-a2" in c.tags}
            <= {i.case.id for i in plan.for_arm("arm0")}
        )

    def test_the_plan_warns_when_the_environment_cannot_show_the_arm(self):
        cfg = {**settings.SEARCH_ENGINE, "AGENT_EFFORT_LEVELS": False, "AGENT_TWO_TIER_LOOP": False}
        with self.settings(SEARCH_ENGINE=cfg):
            plan = self.plan(["t905-a2"])
        text = "\n".join(plan.notes)
        self.assertIn("arm t905-a2: needs AGENT_TWO_TIER_LOOP=True", text)
        self.assertIn("arm t905-a2: needs AGENT_EFFORT_LEVELS=True", text)
        self.assertIn("cases need AGENT_EFFORT_LEVELS=True", text)
        self.assertIn("cases need AGENT_TWO_TIER_LOOP=True", text)

    def test_no_warning_when_the_environment_has_it(self):
        cfg = {
            **settings.SEARCH_ENGINE,
            "AGENT_EFFORT_LEVELS": True,
            "AGENT_TWO_TIER_LOOP": True,
        }
        with self.settings(SEARCH_ENGINE=cfg):
            plan = self.plan(["t905-a2"])
        self.assertEqual([n for n in plan.notes if "WARNING" in n], [])

    def test_the_projection_prices_the_long_cases_higher(self):
        model = harness.planning_model()
        long_case = next(c for c in self.cases if c.id == "t905.bigplan.long.en")
        std = next(c for c in self.cases if c.id == "t905.chat.standard.en")
        self.assertGreater(
            harness.estimate_item(long_case, model)["output_tokens"],
            harness.estimate_item(std, model)["output_tokens"],
        )


class CheckTests(SimpleTestCase):
    def check(self, spec, events, answer=""):
        return {c["name"]: c for c in content_checks.check_pending(spec, events, answer=answer)}

    def ok(self, checks):
        return all(c["ok"] for c in checks.values())

    def test_no_proposal_fails_with_what_was_proposed_instead(self):
        out = self.check({"tool": "create_note"}, [pending("create_task", {})], "I need more info")
        self.assertFalse(out["pending_write"]["ok"])
        self.assertIn("create_task", out["pending_write"]["detail"])
        self.assertIn("I need more info", out["pending_write"]["detail"])
        out = self.check({"tool": "create_note"}, [])
        self.assertIn("nothing", out["pending_write"]["detail"])

    def test_plan_task_bodies_are_judged_each_and_the_milestone_only_in_the_total(self):
        ev = [plan("a" * 300 + ".", "b" * 100 + ".", milestone="m" * 5000)]
        out = self.check(
            {"tool": "create_task_plan", "tasks_at_least": 2, "each_body_chars_at_most": 400}, ev
        )
        self.assertTrue(self.ok(out), out)
        out = self.check({"tool": "create_task_plan", "each_body_chars_at_most": 200}, ev)
        self.assertFalse(out["pending_write.each_body_chars_at_most"]["ok"])
        out = self.check({"tool": "create_task_plan", "total_chars_at_most": 1000}, ev)
        self.assertFalse(out["pending_write.total_chars_at_most"]["ok"], "milestone body counts")
        out = self.check({"tool": "create_task_plan", "tasks_at_least": 3}, ev)
        self.assertFalse(out["pending_write.tasks_at_least"]["ok"])

    def test_long_bodies_need_length_and_headings(self):
        body = (
            "### Summary\n" + "x" * 600 + "\n\n### Motivation\n" + "y" * 600 + "\n\n### AC\n- a.\n"
        )
        spec = {
            "tool": "create_task_plan",
            "each_body_chars_at_least": 1000,
            "each_body_headings_at_least": 3,
        }
        self.assertTrue(self.ok(self.check(spec, [plan(body, body)])))
        self.assertFalse(self.ok(self.check(spec, [plan(body, "### Summary\nshort.")])))

    def test_a_note_is_one_body(self):
        ev = [pending("create_note", {"title": "n", "content_text": "# Spec\n" + "z" * 900 + "."})]
        spec = {"tool": "create_note", "total_chars_at_least": 800, "total_chars_at_most": 1200}
        self.assertTrue(self.ok(self.check(spec, ev)))
        self.assertFalse(
            self.ok(self.check({**spec, "total_chars_at_most": 500}, ev)), "too long for Short"
        )

    def test_japanese_is_counted_in_characters(self):
        body = "### 概要\n" + "検索フィルターを追加する。" * 20
        ev = [pending("create_note", {"content_text": body})]
        out = self.check({"tool": "create_note", "total_chars_at_most": 400}, ev)
        self.assertTrue(self.ok(out), out)
        self.assertEqual(
            out["pending_write.total_chars_at_most"]["detail"].split()[0], str(len(body))
        )

    def test_not_cut_off(self):
        fine = [
            "A sentence.",
            "- a list item without a period",
            "### 見出し",
            "| a | b |",
            "終わり。",
            # MIP-1532: short deliberate bodies end without a full stop; none of these is cut off
            "### Summary\n\nDesign the filter UI",
            "### Summary\n\nRoll out to 10% of users",
            "### Summary\n\nフィルターUIの設計",
            "### Summary\n\nユーザーの10%への展開",
            "Write the tests",
            "テストの作成",
            "概要を書く",
        ]
        cut = [
            "The first paragraph of the",
            "```py\nx = 1",
            "",
            "- Do the th\n\nAnd then the",
            "概要を書き、",
            "Steps (see the",
            "「概要を",
            "Build the API and",
            "フィルターUIの",
            "Roll out to 10% of users,",
        ]
        for body in fine:
            self.assertFalse(content_checks.looks_cut_off(body), body)
        for body in cut:
            self.assertTrue(content_checks.looks_cut_off(body), body)
        ev = [plan("Whole one.", "The second one stops and")]
        out = self.check({"tool": "create_task_plan", "not_cut_off": True}, ev)
        self.assertFalse(out["pending_write.not_cut_off"]["ok"])
        self.assertIn("[1]", out["pending_write.not_cut_off"]["detail"])

    def test_the_old_heuristic_flagged_every_plain_body(self):
        def old(body):  # the pre-MIP-1532 rule, kept here to show what it did
            text = body.rstrip()
            last = text.splitlines()[-1]
            return last[-1].isalnum()

        for body in ["### Summary\n\nDesign the filter UI", "### Summary\n\nテストの作成"]:
            self.assertTrue(old(body))
            self.assertFalse(content_checks.looks_cut_off(body))


class BodyKindTests(SimpleTestCase):
    def check(self, spec, events):
        return {c["name"]: c for c in content_checks.check_pending(spec, events)}

    def test_kinds(self):
        kind = content_checks.body_kind
        self.assertEqual(kind("", "T"), "none")
        self.assertEqual(kind("  \n ", "T"), "none")
        self.assertEqual(kind("### Summary\n\nDesign the filter UI", "Design the filter UI"), "skeleton")
        self.assertEqual(kind("### Summary\n\nフィルターUIの設計", "フィルターUIの設計"), "skeleton")
        self.assertEqual(kind("### Summary", "T"), "skeleton", "a heading and nothing else")
        self.assertEqual(kind("Design the filter UI", "design  the FILTER ui"), "skeleton")
        self.assertEqual(kind("### Summary\n\n**T**", "T"), "skeleton")
        self.assertEqual(
            kind("### Summary\n\nT\n\n### Acceptance criteria\n\n- [ ]\n- \n", "T"), "skeleton"
        )
        self.assertEqual(kind("### Summary\n\nT\n\n<!-- fill in -->", "T"), "skeleton")
        self.assertEqual(kind("### Summary\n\nBuild it for the new API.", "T"), "content")
        self.assertEqual(kind("### Summary\n\nT\n\n- [ ] Cover the empty state", "T"), "content")
        self.assertEqual(kind("### Summary\n\nT is more than a title", "T"), "content")
        self.assertEqual(kind("### Summary\n\nsomething", ""), "content")

    def test_a_skeleton_passes_the_floor_and_is_reported_as_one(self):
        ev = [plan("### Summary\n\nt0", "### Summary\n\nt1")]
        out = self.check(
            {"tool": "create_task_plan", "each_body_kind_at_least": "skeleton", "not_cut_off": True},
            ev,
        )
        self.assertTrue(all(c["ok"] for c in out.values()), out)
        self.assertIn("skeleton 2", out["pending_write.body_kinds"]["detail"])
        self.assertIn("content 0", out["pending_write.body_kinds"]["detail"])

    def test_no_body_fails_the_floor_and_content_floor_separates_the_two(self):
        ev = [plan("### Summary\n\nt0", "")]
        out = self.check({"tool": "create_task_plan", "each_body_kind_at_least": "skeleton"}, ev)
        self.assertFalse(out["pending_write.each_body_kind_at_least"]["ok"])
        self.assertIn("[1]", out["pending_write.each_body_kind_at_least"]["detail"])
        ev = [plan("### Summary\n\nt0", "### Summary\n\nt1\n\nReal prose.")]
        out = self.check({"tool": "create_task_plan", "each_body_kind_at_least": "content"}, ev)
        self.assertFalse(out["pending_write.each_body_kind_at_least"]["ok"])
        self.assertIn("[0]", out["pending_write.each_body_kind_at_least"]["detail"])
        self.assertIn("skeleton 1, content 1", out["pending_write.body_kinds"]["detail"])

    def test_a_body_that_is_the_title_of_another_task_is_content(self):
        ev = [plan("### Summary\n\nt1", "### Summary\n\nt1")]  # tasks are named t0, t1
        out = self.check({"tool": "create_task_plan", "report_body_kinds": True}, ev)
        self.assertIn("skeleton 1, content 1", out["pending_write.body_kinds"]["detail"])

    def test_report_only_never_fails(self):
        out = self.check({"tool": "create_task_plan", "report_body_kinds": True}, [plan("")])
        self.assertTrue(out["pending_write.body_kinds"]["ok"])
        self.assertIn("none 1", out["pending_write.body_kinds"]["detail"])

    def test_an_empty_plan_has_no_bodies_and_fails_the_floor(self):
        ev = [pending("create_task_plan", {"tasks": []})]
        out = self.check({"tool": "create_task_plan", "each_body_kind_at_least": "skeleton"}, ev)
        self.assertFalse(out["pending_write.each_body_kind_at_least"]["ok"])

    def test_t1510_cases_use_the_kind_checks(self):
        cases = [c for c in harness.load_cases(include_legacy=False) if "t1510" in c.tags]
        write = [c for c in cases if "single-task" not in c.id]
        self.assertEqual(len(write), 4)
        for c in write:
            spec = c.raw["expect"]["pending_write"]
            self.assertNotIn("each_body_chars_at_least", spec, c.id)
            self.assertEqual(spec["each_body_kind_at_least"], "skeleton", c.id)
            self.assertTrue(spec["report_body_kinds"], c.id)


    def test_answer_length_is_in_characters(self):
        checks = content_checks.check_answer_length(
            {"answer_chars_at_most": 10, "answer_chars_at_least": 3}, "ステータス"
        )
        self.assertTrue(all(c["ok"] for c in checks))
        checks = content_checks.check_answer_length({"answer_chars_at_most": 4}, "ステータス")
        self.assertFalse(checks[0]["ok"])

    def test_the_proposal_is_rendered_for_the_report(self):
        text = content_checks.render_pending([plan("one.", "two.", milestone="ms.")])
        self.assertIn("[proposed create_task_plan]", text)
        for part in ("ms.", "one.", "two."):
            self.assertIn(part, text)
        self.assertEqual(content_checks.render_pending([]), "")

    def test_group_checks_wires_them_in(self):
        checks = harness.group_checks(
            {"pending_write": {"tool": "create_note"}, "answer_chars_at_most": 5},
            answer="too long an answer",
            tools=[],
            tool_text="",
            calls=1,
            pending=[pending("create_note", {"content_text": "x."})],
        )
        names = {c["name"]: c["ok"] for c in checks}
        self.assertTrue(names["pending_write"])
        self.assertFalse(names["answer_chars_at_most"])
        # a case with none of the keys is not touched
        self.assertEqual(harness.group_checks({}, answer="", tools=[], tool_text="", calls=0), [])


class SupportTests(SimpleTestCase):
    def test_member_prefs_sets_the_level_for_the_block_only(self):
        self.assertEqual(content_length.member_level("not-a-uuid"), "standard")
        with content_checks.member_prefs({"content_length": "long"}):
            self.assertEqual(content_length.member_level("not-a-uuid"), "long")
        self.assertEqual(content_length.member_level("not-a-uuid"), "standard")
        with content_checks.member_prefs(None), content_checks.member_prefs({}):
            self.assertEqual(content_length.member_level("not-a-uuid"), "standard")

    def test_member_prefs_levels_are_per_thread_so_parallel_cases_do_not_race(self):
        seen: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def work(level):
            with content_checks.member_prefs({"content_length": level}):
                barrier.wait(timeout=5)
                seen[level] = content_length.member_level("x")

        threads = [threading.Thread(target=work, args=(lvl,)) for lvl in ("short", "long")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(seen, {"short": "short", "long": "long"})

    def test_case_llm_choice_binds_and_restores(self):
        from origin.search_engine.llm.choice import get_llm_choice

        before = get_llm_choice()
        with content_checks.case_llm_choice({"provider": "gemini", "effort": "low"}):
            choice = get_llm_choice()
            self.assertEqual((choice.provider, choice.effort), ("gemini", "low"))
            self.assertEqual(choice.model, settings.LLM_CATALOG.model_for_effort("gemini", "low"))
        self.assertEqual(get_llm_choice(), before)
        with content_checks.case_llm_choice(None):
            self.assertEqual(get_llm_choice(), before)

    def test_the_pending_events_are_teed_and_the_patch_is_removed(self):
        original = runner.run_agent
        seen = []

        def fake(query, ctx, emit, **kwargs):
            emit(pending("create_note", {"content_text": "body."}))
            emit({"type": "answer_delta", "text": "hi"})

        with mock.patch.object(runner, "run_agent", fake):
            patched_original = runner.run_agent
            with content_checks.capture_pending() as sink:
                self.assertIsNot(runner.run_agent, patched_original)
                runner.run_agent("q", None, seen.append, run_id=None)
            self.assertIs(runner.run_agent, patched_original, "restored when the last case ends")
        self.assertIs(runner.run_agent, original)
        self.assertEqual([e["tool_name"] for e in sink], ["create_note"])
        self.assertEqual([e["type"] for e in seen], ["tool_call_pending_approval", "answer_delta"])

    def test_an_uncaptured_run_passes_straight_through(self):
        calls = []

        def fake(query, ctx, emit, **kwargs):
            calls.append(emit)

        with mock.patch.object(runner, "run_agent", fake):
            with content_checks.capture_pending():
                pass
            runner.run_agent("q", None, "EMIT")
        self.assertEqual(calls, ["EMIT"])


class CaseRunnerIntegrationTests(SimpleTestCase):
    """`DefaultCaseRunner` on a t905 case with the agent stubbed out: the level reaches the
    controller's resolver, the proposal is checked, and the report carries its text."""

    def run_case(self, case_id, body):
        case = next(c for c in harness.load_cases(include_legacy=False) if c.id == case_id)
        levels = []

        def fake_behaviour(raw):
            levels.append(content_length.member_level("x"))
            runner.run_agent(
                raw["query"],
                None,
                lambda e: None,
            )
            return runner.CaseResult(case_id=case_id, passed=True, duration_ms=1, answer="")

        def fake_agent(query, ctx, emit, **kwargs):
            emit(pending("create_note", {"title": "n", "content_text": body}))

        with (
            mock.patch.object(runner, "run_behavior_case", fake_behaviour),
            mock.patch.object(runner, "run_agent", fake_agent),
        ):
            result = case_runner.DefaultCaseRunner()(case, "t905", {})
        return levels, result

    def test_a_short_case_sees_the_short_level_and_its_bound_is_checked(self):
        levels, result = self.run_case(
            "t905.spec-note.short.en", "### Goals\n" + "Be brief and to the point. " * 8
        )
        self.assertEqual(levels, ["short"])
        self.assertTrue(result["passed"], result["checks"])
        self.assertIn("[proposed create_note]", result["answer"])
        self.assertIn("Be brief and to the point.", result["answer"])

    def test_a_short_case_fails_on_a_long_body(self):
        _, result = self.run_case("t905.spec-note.short.en", "word. " * 400)
        self.assertFalse(result["passed"])
        failed = [c["name"] for c in result["checks"] if not c["ok"]]
        self.assertIn("pending_write.total_chars_at_most", failed)

    def test_a_long_case_passes_only_with_a_full_body(self):
        full = (
            "### Goals\n" + "A full sentence about goals. " * 100 + "\n\n" + "### Scope\nS.\n" * 4
        )
        _, result = self.run_case("t905.spec-note.long.en", full)
        self.assertTrue(result["passed"], result["checks"])
        _, result = self.run_case("t905.spec-note.long.en", "### Goals\nBrief.")
        self.assertFalse(result["passed"])

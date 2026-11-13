"""The multi-arm eval harness (`agent_eval_group`, `evals/group/harness.py`).

No model is ever called. Where a call path matters, `llm._build_adapter` is replaced by a stub
that does what every adapter does (`spend.record_llm_call` at the end of a call), and the
harness's own cost gate wraps it exactly as in a paid run.
"""

from __future__ import annotations

import io
import json
import tempfile
import types
from pathlib import Path
from unittest import mock

import yaml
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from origin.search_engine import llm
from origin.search_engine.agent.evals.group import case_runner, harness
from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.agent.evals.group.harness import ALL_ON, ARM0
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import AgentMessage, CallUsage
from origin.search_engine.management.commands import agent_eval_group

MODEL = "gemini-3.5-flash-lite"
FLAG_A = "AGENT_STEP_CAP_WRAPUP"  # default True on this stack: an arm turns it False
FLAG_B = "RAG_USE_RERANKER"  # default True (settings.py): an arm turns it False


def write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def case(cid, *tags, lang="en", **extra):
    return {
        "id": cid,
        "query": "q " + cid,
        "lang": lang,
        "tags": ["eval-group", *tags],
        "team_id": "t",
        "user_id": "u",
        "fixture": "v1",
        **extra,
    }


class StubAdapter:
    """What a provider adapter does, minus the provider."""

    def __init__(self, prompt=1000, output=100):
        self.calls = 0
        self.prompt = prompt
        self.output = output

    def generate_step(
        self,
        messages,
        tools,
        system_instruction,
        *,
        model_override=None,
        usage_sink=None,
        params=None,
    ):
        self.calls += 1
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider, sink.model = "gemini", model_override or MODEL
        sink.prompt_tokens, sink.output_tokens = self.prompt, self.output

        def gen():
            try:
                yield "ok", None
            finally:
                spend.record_llm_call(sink)

        return gen()


class Workspace(SimpleTestCase):
    """Temp group dir + arms dir patched into the harness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.group = Path(self.tmp.name) / "group"
        self.arms_dir = self.group / "arms"
        self.evals = Path(self.tmp.name) / "evals"
        self.evals.mkdir()
        self.group.mkdir()
        for name, path in (
            ("GROUP_DIR", self.group),
            ("ARMS_DIR", self.arms_dir),
            ("EVALS_DIR", self.evals),
        ):
            patcher = mock.patch.object(harness, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stub = StubAdapter()
        patcher = mock.patch.object(llm, "_build_adapter", lambda provider: self.stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arm(self, name, overrides, requires=()):
        write_yaml(
            self.arms_dir / f"{name}.yaml",
            {"arm": name, "overrides": overrides, "requires": list(requires)},
        )

    def cases(self, name, entries):
        write_yaml(self.group / f"{name}.yaml", entries)

    def run_command(self, *argv, runner=None, expect_exit=None):
        out, err = io.StringIO(), io.StringIO()
        factory = (lambda judge: runner) if runner else agent_eval_group.Command.runner_factory
        with mock.patch.object(agent_eval_group.Command, "runner_factory", staticmethod(factory)):
            if expect_exit is None:
                call_command("agent_eval_group", *argv, stdout=out, stderr=err)
            else:
                with self.assertRaises(SystemExit) as caught:
                    call_command("agent_eval_group", *argv, stdout=out, stderr=err)
                self.assertEqual(caught.exception.code, expect_exit)
        return out.getvalue(), err.getvalue()


def flag_runner(passes_when_flag_off=True, flag=FLAG_A):
    """Calls the (stubbed) model once, passes when the flag state matches the expectation."""

    def run(gcase, arm, overrides):
        client = llm.get_model_client()
        list(client.generate_step([AgentMessage(role="user", text="q")], [], "sys"))
        result = group_usage.new_result(gcase.id, arm, gcase.lang)
        flag_on = bool(settings.SEARCH_ENGINE[flag])
        result["passed"] = (not flag_on) if passes_when_flag_off else flag_on
        result["tools"] = ["list_tasks", "get_task"]
        result["checks"] = [{"name": "c", "ok": result["passed"], "detail": f"flag={flag_on}"}]
        return result

    return run


# --------------------------------------------------------------------------- #


class CaseLoadingTests(Workspace):
    def test_old_cases_without_tags_or_lang_load(self):
        write_yaml(
            self.evals / "cases.yaml",
            [{"id": "old", "query": "hello", "fixture": True, "expect": {}}],
        )
        write_yaml(
            self.evals / "routing_cases.yaml",
            [{"id": "r1", "en": "list my tasks", "ja": "タスク", "ok": ["list_tasks"]}],
        )
        cases = {c.id: c for c in harness.load_cases(group_dir=self.group, evals_dir=self.evals)}
        self.assertEqual(cases["old"].lang, "en")
        self.assertEqual(cases["old"].fixture, "v1")
        self.assertIn("legacy", cases["old"].tags)
        self.assertEqual(cases["r1.ja"].lang, "ja")
        self.assertEqual(cases["r1.ja"].kind, "routing")

    def test_group_cases_carry_the_group_tag_and_defaults(self):
        self.cases("t1", [{"id": "g1", "query": "x", "tags": ["t977"]}])
        (c,) = harness.load_cases(group_dir=self.group, evals_dir=self.evals, include_legacy=False)
        self.assertEqual(c.tags, frozenset({"eval-group", "t977"}))
        self.assertEqual((c.kind, c.fixture), ("behaviour", "v1"))

    def test_bad_kind_and_duplicate_ids_are_refused(self):
        self.cases("a", [{"id": "x", "query": "q", "kind": "nonsense"}])
        with self.assertRaises(harness.HarnessError):
            harness.load_cases(group_dir=self.group, evals_dir=self.evals)
        self.cases("a", [{"id": "x", "query": "q"}])
        self.cases("b", [{"id": "x", "query": "q"}])
        with self.assertRaisesMessage(harness.HarnessError, "duplicate case id"):
            harness.load_cases(group_dir=self.group, evals_dir=self.evals)

    def test_the_shipped_files_load(self):
        # The real directory, not the temp one: every shipped case file must parse.
        with mock.patch.object(harness, "GROUP_DIR", Path(harness.__file__).parent):
            cases = harness.load_cases(
                group_dir=Path(harness.__file__).parent,
                evals_dir=Path(harness.__file__).parent.parent,
            )
        self.assertGreater(len(cases), 100)


class SelectionTests(Workspace):
    def setUp(self):
        super().setUp()
        self.cases(
            "x",
            [
                case("a1", "t1"),
                case("a2", "t1", lang="ja"),
                case("b1", "t2"),
                case("r.en", "t1", kind="routing"),
            ],
        )
        write_yaml(self.evals / "cases.yaml", [{"id": "legacy1", "query": "q", "fixture": True}])
        self.all = harness.load_cases(group_dir=self.group, evals_dir=self.evals)

    def ids(self, **kw):
        return [c.id for c in harness.select_cases(self.all, **kw)]

    def test_tag_selects_and_repeats_add_up(self):
        self.assertEqual(self.ids(tags=["t2"]), ["b1"])
        self.assertEqual(self.ids(tags=["t1", "t2"]), ["a1", "a2", "b1", "r.en"])

    def test_case_id_and_base_id(self):
        self.assertEqual(self.ids(case_ids=["a2"]), ["a2"])
        self.assertEqual(self.ids(case_ids=["r"]), ["r.en"])

    def test_lang_filter(self):
        self.assertEqual(self.ids(tags=["t1"], lang="ja"), ["a2"])

    def test_default_is_the_group_cases_only_legacy_on_request(self):
        self.assertNotIn("legacy1", self.ids())
        self.assertIn("legacy1", self.ids(include_legacy=True))

    def test_unknown_tag_or_id_is_an_error_not_an_empty_run(self):
        with self.assertRaises(harness.HarnessError):
            self.ids(tags=["nope"])
        with self.assertRaises(harness.HarnessError):
            self.ids(case_ids=["nope"])

    def test_max_cases(self):
        self.assertEqual(self.ids(tags=["t1"], max_cases=2), ["a1", "a2"])


class ArmTests(Workspace):
    def test_arms_load_and_requires_pulls_in_overrides(self):
        self.arm("t1", {FLAG_A: False})
        self.arm("t2", {FLAG_B: False}, requires=["t1"])
        arms = harness.load_arms(self.arms_dir)
        self.assertEqual(arms["t2"].effective, {FLAG_A: False, FLAG_B: False})
        self.assertEqual(arms["t2"].satisfies, frozenset({"t1", "t2"}))
        self.assertEqual(arms["t1"].effective, {FLAG_A: False})

    def test_all_on_is_the_union(self):
        self.arm("t1", {FLAG_A: False})
        self.arm("t2", {FLAG_B: False})
        self.assertEqual(
            harness.all_on_arm(harness.load_arms(self.arms_dir)).effective,
            {FLAG_A: False, FLAG_B: False},
        )

    def test_two_arms_that_disagree_cannot_be_unioned(self):
        self.arm("t1", {FLAG_A: False})
        self.arm("t2", {FLAG_A: True})
        with self.assertRaisesMessage(harness.HarnessError, "all-on"):
            harness.all_on_arm(harness.load_arms(self.arms_dir))

    def test_cycles_unknown_requires_reserved_and_name_mismatch(self):
        self.arm("t1", {}, requires=["t2"])
        self.arm("t2", {}, requires=["t1"])
        with self.assertRaisesMessage(harness.HarnessError, "cycle"):
            harness.load_arms(self.arms_dir)
        for stem in ("t1", "t2"):
            (self.arms_dir / f"{stem}.yaml").unlink()
        self.arm("t3", {}, requires=["ghost"])
        with self.assertRaisesMessage(harness.HarnessError, "unknown arm"):
            harness.load_arms(self.arms_dir)
        (self.arms_dir / "t3.yaml").unlink()
        write_yaml(self.arms_dir / "x.yaml", {"arm": "y", "overrides": {}})
        with self.assertRaisesMessage(harness.HarnessError, "must equal the file name"):
            harness.load_arms(self.arms_dir)

    def test_unknown_override_key_is_refused(self):
        arm = harness.Arm(
            name="typo",
            own={"AGENT_NOT_A_FLAG": True},
            effective={"AGENT_NOT_A_FLAG": True},
            source="typo.yaml",
        )
        (problem,) = harness.validate_overrides(arm)
        self.assertIn("unknown SEARCH_ENGINE key 'AGENT_NOT_A_FLAG'", problem)

    def test_an_override_equal_to_the_current_value_is_refused(self):
        current = settings.SEARCH_ENGINE[FLAG_A]
        arm = harness.Arm(name="noop", effective={FLAG_A: current}, source="noop.yaml")
        (problem,) = harness.validate_overrides(arm)
        self.assertIn("changes nothing", problem)

    def test_apply_overrides_restores_even_on_error(self):
        before = settings.SEARCH_ENGINE[FLAG_A]
        with self.assertRaises(RuntimeError):
            with harness.apply_overrides({FLAG_A: not before}):
                self.assertEqual(settings.SEARCH_ENGINE[FLAG_A], not before)
                raise RuntimeError("boom")
        self.assertEqual(settings.SEARCH_ENGINE[FLAG_A], before)

    def test_apply_overrides_refuses_an_unknown_key_before_touching_anything(self):
        before = settings.SEARCH_ENGINE[FLAG_A]
        with self.assertRaises(harness.HarnessError):
            with harness.apply_overrides({FLAG_A: not before, "AGENT_NOT_A_FLAG": 1}):
                self.fail("must not enter")
        self.assertEqual(settings.SEARCH_ENGINE[FLAG_A], before)
        self.assertNotIn("AGENT_NOT_A_FLAG", settings.SEARCH_ENGINE)


class PlanTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.arm("t2", {FLAG_B: False})
        self.cases(
            "x",
            [
                case("a", "t1"),
                case("b", "t2"),
                case("c", "t1", "t2", reps=3),
                case("needs", "t1", requires_arm="t2"),
            ],
        )
        self.cases_all = harness.load_cases(
            group_dir=self.group, evals_dir=self.evals, include_legacy=False
        )
        self.arms = harness.load_arms(self.arms_dir)

    def runs(self, plan, arm):
        return sorted(
            f"{i.case.id}#{i.rep}" + ("(skip)" if i.skip else "") for i in plan.for_arm(arm)
        )

    def test_baseline_runs_once_per_case_and_item_arms_only_their_own_tag(self):
        plan = harness.build_plan(self.cases_all, self.arms, reps=1)
        self.assertEqual(
            self.runs(plan, ARM0), ["a#1", "b#1", "c#1", "c#2", "c#3", "needs#1(skip)"]
        )
        self.assertEqual(self.runs(plan, "t1"), ["a#1", "c#1", "c#2", "c#3"])
        self.assertEqual(self.runs(plan, "t2"), ["b#1", "c#1", "c#2", "c#3", "needs#1"])
        self.assertEqual(self.runs(plan, ALL_ON), ["a#1", "b#1", "c#1", "c#2", "c#3", "needs#1"])

    def test_the_baseline_is_not_repeated_per_arm(self):
        plan = harness.build_plan(self.cases_all, self.arms)
        for item in plan.runnable():
            if item.arm.name == ARM0:
                self.assertEqual(
                    [
                        i
                        for i in plan.runnable()
                        if i.arm.name == ARM0 and i.case is item.case and i.rep == item.rep
                    ],
                    [item],
                )

    def test_reps_default_and_per_case_override(self):
        plan = harness.build_plan(self.cases_all, self.arms, reps=2)
        self.assertEqual(self.runs(plan, "t2")[:2], ["b#1", "b#2"])
        self.assertEqual(
            [r for r in self.runs(plan, ARM0) if r.startswith("c#")], ["c#1", "c#2", "c#3"]
        )

    def test_requires_arm_is_skipped_on_the_baseline_at_no_cost(self):
        plan = harness.build_plan(self.cases_all, self.arms)
        self.assertIn("needs#1(skip)", self.runs(plan, ARM0))
        skipped = [i for i in plan.items if i.skip]
        self.assertTrue(all(i.skip.startswith("requires_arm") for i in skipped))
        cost = harness.project_cost(plan, MODEL)
        self.assertEqual(cost["items"], len(plan.runnable()))

    def test_arm_selection_and_unknown_arm(self):
        plan = harness.build_plan(self.cases_all, self.arms, arm_names=["t2"])
        self.assertEqual([a.name for a in plan.arms], [ARM0, "t2"])
        with self.assertRaisesMessage(harness.HarnessError, "unknown arm"):
            harness.build_plan(self.cases_all, self.arms, arm_names=["zzz"])

    def test_requires_arm_without_an_arm_file_is_skipped_on_all_on_too(self):
        self.cases("y", [case("lonely", "t9", requires_arm="t9")])
        cases = harness.load_cases(group_dir=self.group, evals_dir=self.evals, include_legacy=False)
        plan = harness.build_plan(cases, self.arms)
        self.assertTrue(all(i.skip for i in plan.items if i.case.id == "lonely"))

    def test_projection_prices_the_catalog_model(self):
        cost = harness.project_cost(harness.build_plan(self.cases_all, self.arms), MODEL)
        self.assertGreater(cost["cost_usd"], 0)
        self.assertFalse(cost["unpriced"])
        self.assertTrue(
            harness.project_cost(harness.build_plan(self.cases_all, self.arms), "no-such-model")[
                "unpriced"
            ]
        )


class DryRunTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.cases(
            "x",
            [
                case("a", "t1"),
                case("s", "t1", fixture="sequence", viewer="v2"),
                case("r", "t1", kind="resume"),
            ],
        )

    def test_dry_run_makes_no_model_call_touches_no_fixture_and_needs_no_credentials(self):
        def boom(*a, **k):
            raise AssertionError("dry-run must not do this")

        with (
            mock.patch.object(llm, "_build_adapter", boom),
            mock.patch("origin.search_engine.agent.evals.fixture.ensure_fixture", boom),
            mock.patch("origin.search_engine.agent.evals.fixture.reseed_fixture", boom),
            mock.patch.object(case_runner, "resolve_sequence", boom),
            mock.patch("origin.search_engine.llm.get_model_client", boom),
        ):
            out, _ = self.run_command("--dry-run", "--tag", "t1")
        self.assertIn("dry run: no model call", out)
        self.assertIn("TOTAL:", out)
        self.assertIn("arm0", out)
        self.assertIn("all-on", out)
        self.assertIn(f"priced on model: {harness.planning_model()}", out)
        self.assertIn(harness.ROUTING_MODEL, out)

    def test_dry_run_reports_runs_calls_and_cost_per_arm(self):
        out, _ = self.run_command("--dry-run", "--tag", "t1", "--reps", "2")
        # 3 cases x 2 reps on arm0, the same on t1 and on all-on
        self.assertIn("| arm0 | - | 6 |", out)
        self.assertIn("| t1 |", out)

    def test_a_real_run_without_a_cap_is_refused(self):
        with self.assertRaisesMessage(CommandError, "--cost-cap-usd"):
            self.run_command("--tag", "t1", runner=flag_runner())

    def test_an_unknown_flag_arm_is_refused_and_can_be_skipped(self):
        self.arm("typo", {"AGENT_NOT_A_FLAG": True})
        self.cases("z", [case("tz", "typo")])
        with self.assertRaisesMessage(CommandError, "unknown SEARCH_ENGINE key"):
            self.run_command("--dry-run", "--tag", "typo")
        out, _ = self.run_command("--dry-run", "--tag", "typo", "--skip-unavailable")
        self.assertIn("DROPPED", out)


class RunTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.arm("t2", {FLAG_B: False})
        self.cases("x", [case("a", "t1"), case("b", "t2"), case("c", "t1", lang="ja")])
        self.before = (settings.SEARCH_ENGINE[FLAG_A], settings.SEARCH_ENGINE[FLAG_B])
        self.recorder_before = spend._recorder
        self.adapter_before = llm._build_adapter

    def tearDown(self):
        self.assertEqual(
            (settings.SEARCH_ENGINE[FLAG_A], settings.SEARCH_ENGINE[FLAG_B]), self.before
        )
        # the harness leaves no recorder tee and no adapter gate behind
        self.assertIs(spend._recorder, self.recorder_before)
        self.assertIs(llm._build_adapter, self.adapter_before)

    def test_matrix_totals_delta_jsonl_and_overrides_restored(self):
        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "run.jsonl"
            out, _ = self.run_command(
                "--tag",
                "t1",
                "--tag",
                "t2",
                "--cost-cap-usd",
                "5",
                "--json",
                str(out_path),
                runner=flag_runner(passes_when_flag_off=False),  # passes only while FLAG_A is ON
            )
            lines = [json.loads(line) for line in out_path.read_text().splitlines()]
            self.assertTrue(out_path.with_suffix(".report.md").exists())
            summary = json.loads(out_path.with_suffix(".summary.json").read_text())
        self.assertIn("| case | lang | arm0 | t1 | t2 | all-on |", out)
        # arm0 runs all 3, t1 only a+c, t2 only b, all-on all 3: 3 + 2 + 1 + 3 model calls
        self.assertEqual(self.stub.calls, 9)
        self.assertEqual(len(lines), 9)
        self.assertEqual({r["arm"] for r in lines}, {ARM0, "t1", "t2", ALL_ON})
        self.assertTrue(all(r["usage"]["calls"] == 1 and r["usage"]["cost_usd"] > 0 for r in lines))
        self.assertEqual(summary["arms"]["arm0"]["passed"], 3)
        # t1 turns FLAG_A off: the runner passes only while it is on -> both t1 cases regress
        self.assertEqual(sorted(summary["regressions"]["t1"]), ["a", "c"])
        self.assertEqual(summary["arms"]["t1"]["vs_arm0"]["pass_delta"], -2)
        self.assertEqual(summary["arms"]["t1"]["vs_arm0"]["cases"], 2)
        # the bisect hint names t1 for the case it regressed on all-on
        hints = {h["case_id"]: h for h in summary["bisect"]}
        self.assertEqual(hints["a"]["culprits"], ["t1"])
        self.assertIn("Bisect hints", out)
        self.assertIn("regressed by arm(s): t1", out)

    def test_overrides_are_restored_when_the_runner_raises(self):
        def crash(gcase, arm, overrides):
            raise RuntimeError("runner exploded")

        out, _ = self.run_command(
            "--tag", "t1", "--cost-cap-usd", "5", "--arms", "t1", runner=crash
        )
        self.assertIn("RuntimeError: runner exploded", out)
        # tearDown asserts the flags are back

    def test_baseline_result_is_reused_not_rerun_per_arm(self):
        seen = []

        def runner(gcase, arm, overrides):
            seen.append((gcase.id, arm))
            return flag_runner()(gcase, arm, overrides)

        self.run_command("--tag", "t1", "--tag", "t2", "--cost-cap-usd", "5", runner=runner)
        self.assertEqual(sum(1 for cid, arm in seen if cid == "a" and arm == ARM0), 1)
        self.assertEqual(len(seen), len(set(seen)))

    def test_cost_cap_stops_before_the_next_call_and_the_partial_report_says_so(self):
        # one call costs 1000*0.30/1e6 + 100*2.5/1e6 = $0.00055; a cap of $0.0012 allows two.
        small = {"calls": 1, "input_tokens": 1000, "cached_tokens": 0, "output_tokens": 100}
        self.cases("x", [case(f"k{i}", "t1", est=small) for i in range(5)])
        out, err = self.run_command(
            "--tag",
            "t1",
            "--cost-cap-usd",
            "0.0012",
            "--model",
            MODEL,
            runner=flag_runner(),
            expect_exit=3,
        )
        self.assertIn("ABORTED", out)
        self.assertIn("not run: cost cap", out)
        self.assertEqual(self.stub.calls, 2)  # the third would cross the cap, so it never starts
        self.assertIn("ABORTED", err)

    def test_cap_is_checked_before_every_model_call_inside_a_case(self):
        guard = harness.SessionSpend(0.001)
        guard._spent, guard._calls = 0.001, 2  # already at the cap
        with harness.guarded_llm(guard):
            client = llm.get_model_client()
            with self.assertRaises(harness.CostCapReached):
                client.generate_step([AgentMessage(role="user", text="q")], [], "s")
        self.assertEqual(self.stub.calls, 0)

    def test_a_run_with_a_stop_file_stops_cleanly(self):
        with tempfile.NamedTemporaryFile() as stop:
            out, _ = self.run_command(
                "--tag",
                "t1",
                "--cost-cap-usd",
                "5",
                "--stop-file",
                stop.name,
                runner=flag_runner(),
                expect_exit=3,
            )
        self.assertEqual(self.stub.calls, 0)
        self.assertIn("stop file", out)

    def test_concurrency_runs_cases_in_parallel_within_an_arm_without_mixing_usage(self):
        out_path = Path(tempfile.mkdtemp()) / "c.jsonl"
        self.run_command(
            "--tag",
            "t1",
            "--tag",
            "t2",
            "--cost-cap-usd",
            "5",
            "--concurrency",
            "3",
            "--json",
            str(out_path),
            runner=flag_runner(),
        )
        rows = [json.loads(line) for line in out_path.read_text().splitlines()]
        self.assertEqual(len(rows), 9)
        self.assertTrue(all(r["usage"]["calls"] == 1 for r in rows))

    def test_measure_one_runs_one_case_on_arm0_and_projects_the_plan(self):
        out, _ = self.run_command(
            "--tag",
            "t1",
            "--tag",
            "t2",
            "--measure-one",
            "--cost-cap-usd",
            "5",
            runner=flag_runner(),
        )
        self.assertEqual(self.stub.calls, 1)
        self.assertIn("measured: calls=1 input_tokens=1000", out)
        self.assertIn("PROJECTION for the whole plan", out)

    def test_max_cases_limits_the_selection(self):
        self.run_command(
            "--tag",
            "t1",
            "--tag",
            "t2",
            "--max-cases",
            "1",
            "--arms",
            "t1",
            "--cost-cap-usd",
            "5",
            runner=flag_runner(),
        )
        self.assertEqual(self.stub.calls, 2)  # arm0 + t1 on the first case only

    def test_reps_repeat_a_case(self):
        self.run_command(
            "--tag",
            "t2",
            "--reps",
            "3",
            "--arms",
            "t2",
            "--cost-cap-usd",
            "5",
            runner=flag_runner(),
        )
        self.assertEqual(self.stub.calls, 6)

    def test_unpriced_model_is_flagged_in_the_report(self):
        class Unpriced(StubAdapter):
            def generate_step(self, *a, **k):
                k["model_override"] = "mystery-model"
                return super().generate_step(*a, **k)

        self.stub = Unpriced()
        out, _ = self.run_command(
            "--tag", "t2", "--arms", "t2", "--cost-cap-usd", "5", runner=flag_runner()
        )
        self.assertIn("lower bound", out)


class ResumeDispatchTests(Workspace):
    def setUp(self):
        super().setUp()
        self.gcase = harness.GroupCase(
            id="res1",
            kind="resume",
            lang="en",
            tags=frozenset({"eval-group", "t977"}),
            fixture="v1",
            raw={"id": "res1", "kind": "resume", "x": 1},
            source="t977.yaml",
        )

    def test_a_missing_resume_runner_skips_the_case(self):
        missing = ModuleNotFoundError(name=f"{case_runner.__package__}.resume_runner")
        with mock.patch("importlib.import_module", side_effect=missing):
            result = case_runner.DefaultCaseRunner()(self.gcase, ARM0, {})
        self.assertEqual(result["skipped"], "resume_runner missing")

    def test_a_present_resume_runner_gets_the_case_and_the_overrides(self):
        seen = {}

        def run_resume_case(case, overrides):
            seen.update(case=case, overrides=overrides)
            return {
                "passed": True,
                "checks": [],
                "tools": ["x"],
                "answer": "a",
                "usage": group_usage.blank_usage(),
            }

        fake = types.SimpleNamespace(run_resume_case=run_resume_case)
        with mock.patch("importlib.import_module", return_value=fake):
            result = case_runner.DefaultCaseRunner()(self.gcase, "t977", {FLAG_A: False})
        self.assertEqual(seen["overrides"], {FLAG_A: False})
        self.assertEqual(seen["case"]["x"], 1)
        self.assertTrue(result["passed"])
        self.assertEqual(result["arm"], "t977")


class GroupCheckTests(SimpleTestCase):
    def test_facts_must_all_appear_in_order(self):
        expect = {"facts_in_order": [["asked", "requested"], "task", "closed"]}
        ok = harness.group_checks(
            expect,
            answer="The client asked, a task was made, then closed.",
            tools=[],
            tool_text="",
            calls=1,
        )
        self.assertTrue(ok[0]["ok"])
        bad = harness.group_checks(
            expect,
            answer="It was closed. A task. The client requested it.",
            tools=[],
            tool_text="",
            calls=1,
        )
        self.assertFalse(bad[0]["ok"])
        self.assertIn("out of order", bad[0]["detail"])
        miss = harness.group_checks(
            expect, answer="The client asked.", tools=[], tool_text="", calls=1
        )
        self.assertIn("missing", miss[0]["detail"])

    def test_tools_in_order_is_a_subsequence(self):
        expect = {"tools_in_order": ["list_tasks", "get_task"]}
        self.assertTrue(
            harness.group_checks(
                expect, answer="", tools=["list_tasks", "x", "get_task"], tool_text="", calls=1
            )[0]["ok"]
        )
        self.assertFalse(
            harness.group_checks(
                expect, answer="", tools=["get_task", "list_tasks"], tool_text="", calls=1
            )[0]["ok"]
        )

    def test_leak_into_a_tool_result_and_max_calls(self):
        expect = {"no_tool_results_contain": ["missing permission"], "max_calls": 2}
        checks = harness.group_checks(
            expect, answer="", tools=[], tool_text='{"x": "the Missing Permission"}', calls=3
        )
        self.assertEqual([c["ok"] for c in checks], [False, False])


class BisectTests(SimpleTestCase):
    def result(self, cid, arm, passed):
        r = group_usage.new_result(cid, arm)
        r["passed"] = passed
        return r

    def arms(self, *names):
        return [
            harness.baseline_arm(),
            *[harness.Arm(name=n) for n in names],
            harness.Arm(name=ALL_ON),
        ]

    def test_interaction_when_every_arm_alone_passes(self):
        results = [
            self.result("c", ARM0, True),
            self.result("c", "t1", True),
            self.result("c", "t2", True),
            self.result("c", ALL_ON, False),
        ]
        (hint,) = harness.bisect_hints(results, self.arms("t1", "t2"))
        self.assertEqual(hint["culprits"], [])
        self.assertIn("interaction", hint["hint"])

    def test_untested_when_no_item_arm_ran_it(self):
        results = [self.result("c", ARM0, True), self.result("c", ALL_ON, False)]
        (hint,) = harness.bisect_hints(results, self.arms("t1"))
        self.assertIn("no item arm ran this case", hint["hint"])

    def test_no_hint_without_a_regression(self):
        results = [self.result("c", ARM0, False), self.result("c", ALL_ON, True)]
        self.assertEqual(harness.bisect_hints(results, self.arms("t1")), [])


class SequencePlaceholderTests(SimpleTestCase):
    def test_a_sequence_case_resolves_its_own_team_and_drops_the_fixture_key(self):
        gcase = harness.GroupCase(
            id="s",
            kind="behaviour",
            lang="en",
            tags=frozenset({"eval-group"}),
            fixture="sequence",
            raw={
                "id": "s",
                "query": "what about {display_id}?",
                "fixture": "sequence",
                "viewer": "v2",
                "expect": {},
            },
            source="x.yaml",
        )
        info = {"team_id": "TEAM", "user_id": "USER", "display_id": "CLP-1", "task_id": 7}
        with mock.patch.object(case_runner, "resolve_sequence", return_value=info) as resolve:
            raw = case_runner.prepare_case(gcase)
        resolve.assert_called_once_with("v2")
        self.assertEqual(
            (raw["team_id"], raw["user_id"], raw["query"]), ("TEAM", "USER", "what about CLP-1?")
        )
        self.assertNotIn("fixture", raw)  # the runner must not resolve the v1 team over it
        self.assertNotIn("viewer", raw)

    def test_a_v1_case_keeps_the_runners_own_fixture_flag(self):
        gcase = harness.GroupCase(
            id="v",
            kind="behaviour",
            lang="en",
            tags=frozenset(),
            fixture="v1",
            raw={"id": "v", "query": "q", "fixture": "v1", "tags": ["x"], "expect": {}},
            source="x.yaml",
        )
        raw = case_runner.prepare_case(gcase)
        self.assertIs(raw["fixture"], True)
        self.assertNotIn("tags", raw)


class ScriptedAdapter(StubAdapter):
    """Turn 1: call list_tasks. Turn 2: answer. Every call is recorded like an adapter's."""

    def generate_step(
        self,
        messages,
        tools,
        system_instruction,
        *,
        model_override=None,
        usage_sink=None,
        params=None,
    ):
        self.calls += 1
        n = self.calls
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider, sink.model = "gemini", model_override or MODEL
        sink.prompt_tokens, sink.cached_tokens, sink.output_tokens = 800, 200, 50

        def gen():
            try:
                if n == 1:
                    from origin.search_engine.llm.types import FunctionCall

                    yield None, FunctionCall(name="list_tasks", args={})
                else:
                    yield "Nothing is open right now.", None
            finally:
                spend.record_llm_call(sink)

        return gen()


from origin.tests.test_base import BaseAPITestCase  # noqa: E402


class RealRunnerWiringTests(BaseAPITestCase):
    """The default runner, through the REAL `run_behavior_case` and `run_agent`, on a stub model."""

    def test_tools_answer_and_usage_come_through_the_production_loop(self):
        adapter = ScriptedAdapter()
        gcase = harness.GroupCase(
            id="wire1",
            kind="behaviour",
            lang="en",
            tags=frozenset({"eval-group"}),
            fixture="",
            raw={
                "id": "wire1",
                "query": "what is open?",
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "expect": {
                    "tools_used_contains": ["list_tasks"],
                    "tools_in_order": ["list_tasks"],
                    "facts_in_order": ["nothing is open"],
                    "max_calls": 2,
                },
            },
            source="t.yaml",
        )
        guard = harness.SessionSpend(1.0)
        with mock.patch.object(llm, "_build_adapter", lambda provider: adapter):
            with group_usage.listen(guard.on_record), harness.guarded_llm(guard):
                with group_usage.capture_usage() as cap:
                    result = case_runner.DefaultCaseRunner()(gcase, ARM0, {})
        self.assertEqual(result["tools"], ["list_tasks"])
        self.assertIn("Nothing is open", result["answer"])
        self.assertTrue(result["passed"], result["checks"])
        self.assertEqual(
            [c["name"] for c in result["checks"]][-3:],
            ["facts_in_order", "tools_in_order", "max_calls"],
        )
        # the controller made two model calls; the capture saw both, with the cached split
        self.assertEqual(cap.as_dict()["calls"], 2)
        self.assertEqual(cap.as_dict()["cached_tokens"], 400)
        self.assertEqual(cap.as_dict()["input_tokens"], 2000)
        self.assertGreater(guard.spent, 0)
        self.assertAlmostEqual(guard.spent, cap.as_dict()["cost_usd"], places=6)


class RecordingAdapter(StubAdapter):
    """Records the system instruction of every call; answers in text, no tools."""

    def __init__(self):
        super().__init__()
        self.systems: list[str] = []

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.systems.append(system_instruction)
        return super().generate_step(messages, tools, system_instruction, **kw)


class LocaleParityTests(BaseAPITestCase):
    """A `lang: ja` group case runs with the directive production gives a ja member (MIP-1271)."""

    def run_case(self, lang, *, kind="behaviour", **runner_kw):
        adapter = RecordingAdapter()
        raw = {"id": f"loc-{lang}", "query": "what is open?" if lang == "en" else "未完了は?"}
        if kind == "behaviour":
            raw.update(team_id=str(self.team.team_id), user_id=str(self.user.id), expect={})
        else:
            raw.update(ok=["list_tasks"])
        gcase = harness.GroupCase(
            id=raw["id"],
            kind=kind,
            lang=lang,
            tags=frozenset({"eval-group"}),
            fixture="",
            raw=raw,
            source="t.yaml",
        )
        with mock.patch.object(llm, "_build_adapter", lambda provider: adapter):
            case_runner.DefaultCaseRunner(**runner_kw)(gcase, ARM0, {})
        return adapter.systems

    def test_what_production_does_for_a_ja_member(self):
        from origin.search_engine.agent.languages import language_directive
        from origin.search_engine.agent.tools import ToolContext

        # the view builds ToolContext(locale=<ja>) and the controller appends this line
        self.assertEqual(ToolContext(team_id="t", user_id="u", locale="ja").locale, "ja")
        self.assertIn("Write your reply to the user in Japanese", language_directive("ja"))
        self.assertEqual(language_directive("en"), "")

    def test_a_ja_behaviour_case_gets_the_production_directive_and_an_en_case_does_not(self):
        from origin.search_engine.agent.languages import language_directive

        directive = language_directive("ja")
        ja = self.run_case("ja")
        en = self.run_case("en")
        self.assertTrue(ja and all(directive in s for s in ja))
        self.assertTrue(en and all("Write your reply to the user in" not in s for s in en))
        # byte-for-byte what production appends: the base prompt, then the directive last
        self.assertTrue(ja[0].endswith(directive))

    def test_a_ja_routing_case_gets_it_and_no_locale_switches_it_off(self):
        from origin.search_engine.agent.languages import language_directive

        directive = language_directive("ja")
        self.assertIn(directive, self.run_case("ja", kind="routing")[0])
        self.assertNotIn(directive, self.run_case("ja", kind="routing", apply_locale=False)[0])
        self.assertNotIn(directive, self.run_case("ja", apply_locale=False)[0])

    def test_a_resume_case_is_told_its_locale(self):
        seen = {}
        fake = types.SimpleNamespace(
            run_resume_case=lambda case, overrides: seen.update(case=case) or {"passed": True}
        )
        gcase = harness.GroupCase(
            id="r",
            kind="resume",
            lang="ja",
            tags=frozenset(),
            fixture="v1",
            raw={"id": "r", "kind": "resume"},
            source="t.yaml",
        )
        with mock.patch("importlib.import_module", return_value=fake):
            case_runner.DefaultCaseRunner()(gcase, ARM0, {})
            self.assertEqual(seen["case"]["locale"], "ja")
            case_runner.DefaultCaseRunner(apply_locale=False)(gcase, ARM0, {})
            self.assertEqual(seen["case"]["locale"], "")

    def test_the_legacy_runner_is_untouched_by_the_group_layer(self):
        """agent_eval / agent_eval_compare call run_behavior_case directly: no locale, ever."""
        from origin.search_engine.agent.evals import runner
        from origin.search_engine.agent.languages import language_directive

        original = runner.ToolContext
        self.run_case("ja")
        self.assertIs(runner.ToolContext, original)  # patch removed after the case
        adapter = RecordingAdapter()
        with mock.patch.object(llm, "_build_adapter", lambda provider: adapter):
            runner.run_behavior_case(
                {
                    "id": "legacy",
                    "query": "未完了は?",
                    "team_id": str(self.team.team_id),
                    "user_id": str(self.user.id),
                    "expect": {},
                }
            )
        self.assertTrue(adapter.systems)
        self.assertNotIn(language_directive("ja"), adapter.systems[0])
        with case_runner.case_locale("ja"):
            self.assertIsNot(runner.ToolContext, original)
        self.assertIs(runner.ToolContext, original)

    def test_the_patch_survives_concurrent_cases_of_both_languages(self):
        from concurrent.futures import ThreadPoolExecutor

        from origin.search_engine.agent.evals import runner

        original = runner.ToolContext

        def build(lang):
            with case_runner.case_locale(lang):
                import time

                time.sleep(0.02)
                return runner.ToolContext(team_id="t", user_id="u").locale

        with ThreadPoolExecutor(max_workers=4) as pool:
            got = list(pool.map(build, ["ja", "en", "ja", "en", "ja", "en"]))
        self.assertEqual(got, ["ja", "en", "ja", "en", "ja", "en"])
        self.assertIs(runner.ToolContext, original)


class LocaleWarningTests(Workspace):
    def test_dry_run_warns_only_when_a_ja_case_would_run_without_the_locale(self):
        self.cases("x", [case("j1", "t1", lang="ja"), case("e1", "t1")])
        out, _ = self.run_command("--dry-run", "--tag", "t1")
        self.assertNotIn("WARNING: ", out)
        out, _ = self.run_command("--dry-run", "--tag", "t1", "--no-locale")
        self.assertIn("1 selected case(s) have lang: ja but --no-locale is set", out)

    def test_dry_run_warns_when_resume_runner_ignores_the_locale(self):
        self.cases("x", [case("r1", "t1", lang="ja", kind="resume")])
        with mock.patch.object(agent_eval_group, "_resume_runner_reads_locale", return_value=False):
            out, _ = self.run_command("--dry-run", "--tag", "t1")
        self.assertIn("resume_runner does not read `locale`", out)

    def test_an_en_only_selection_never_warns(self):
        self.cases("x", [case("e1", "t1")])
        out, _ = self.run_command("--dry-run", "--tag", "t1", "--no-locale")
        self.assertNotIn("WARNING: ", out)

"""MIP-1504: what a paid grouped-eval run needs around its cases. NO real model.

  * a result row keeps WHAT THE MODEL PROPOSED (tool and truncated arguments), so a failed or
    empty write case explains itself;
  * the command makes `testserver` an allowed host itself (resume cases drive the real views
    through the test client) and puts the list back;
  * a real run refuses, before its first paid call, when the OpenSearch alias is not an alias or
    maps no `knn_vector` `embedding` (the dev compose alias is a plain index: vector search 400s
    and the whole run measures an agent that cannot retrieve);
  * `expect_baseline` shows a baseline that fails BY DESIGN as expected, in the matrix, the
    report and the JSONL row, and changes nothing else.

The resume-leg usage fix (the cost meter on) is pinned in `test_eval_group_resume_runner`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management.base import CommandError
from django.http import HttpRequest
from django.test import SimpleTestCase, override_settings

from origin.search_engine import llm
from origin.search_engine.agent.evals.group import case_runner, content_checks, harness
from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.agent.evals.group.harness import ARM0
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage, FunctionCall
from origin.tests.test_agent_eval_group import (
    FLAG_A,
    MODEL,
    StubAdapter,
    Workspace,
    case,
    flag_runner,
)
from origin.tests.test_base import BaseAPITestCase

# --------------------------------------------------------------------------- #
# 2. The row keeps what the model proposed                                    #
# --------------------------------------------------------------------------- #


def proposal(tool, arguments):
    return {"type": "tool_call_pending_approval", "tool_name": tool, "arguments": arguments}


class SummarizeProposalsTests(SimpleTestCase):
    def test_tool_and_arguments_are_kept(self):
        out = content_checks.summarize_proposals([proposal("create_task", {"title": "T", "n": 1})])
        self.assertEqual(out, [{"tool": "create_task", "args": '{"n": 1, "title": "T"}'}])

    def test_long_arguments_are_cut_with_a_mark(self):
        out = content_checks.summarize_proposals(
            [proposal("create_note", {"content_text": "z" * 900})], limit=300
        )
        self.assertEqual(len(out[0]["args"]), 301)
        self.assertTrue(out[0]["args"].endswith("…"))

    def test_japanese_is_not_escaped_and_a_proposal_without_bodies_still_shows(self):
        out = content_checks.summarize_proposals(
            [proposal("create_task_plan", {"tasks": [{"title": "調査", "content_markdown": ""}]})]
        )
        self.assertIn("調査", out[0]["args"])
        # `render_pending` shows bodies only: this proposal was invisible in the old row
        self.assertEqual(
            content_checks.render_pending(
                [proposal("create_task_plan", {"tasks": [{"title": "調査"}]})]
            ),
            "",
        )

    def test_nothing_proposed_is_an_empty_list(self):
        self.assertEqual(content_checks.summarize_proposals([]), [])
        self.assertEqual(content_checks.summarize_proposals(None), [])


class WriteAdapter(StubAdapter):
    """The model proposes a write on its first call and says nothing."""

    def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kw):
        self.calls += 1
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider, sink.model = "gemini", MODEL
        sink.prompt_tokens, sink.output_tokens = 500, 40

        def gen():
            try:
                yield (
                    None,
                    FunctionCall(
                        name="create_task",
                        args={"title": "Plan the launch", "content_markdown": ""},
                    ),
                )
            finally:
                spend.record_llm_call(sink)

        return gen()


class ProposedRowTests(BaseAPITestCase):
    def test_a_behaviour_row_carries_the_proposed_write_with_empty_bodies(self):
        gcase = harness.GroupCase(
            id="prop1",
            kind="behaviour",
            lang="en",
            tags=frozenset({"eval-group"}),
            fixture="",
            raw={
                "id": "prop1",
                "query": "make a task to plan the launch",
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "expect": {},
            },
            source="t.yaml",
        )
        with mock.patch.object(llm, "_build_adapter", lambda provider: WriteAdapter()):
            result = case_runner.DefaultCaseRunner()(gcase, ARM0, {})
        self.assertTrue(result.get("proposed"), result)
        self.assertEqual(result["proposed"][0]["tool"], "create_task")
        self.assertIn("Plan the launch", result["proposed"][0]["args"])
        self.assertLessEqual(
            len(result["proposed"][0]["args"]), group_usage.PROPOSED_ARGS_CHARS + 1
        )

    def test_a_row_without_a_proposal_is_unchanged(self):
        gcase = harness.GroupCase(
            id="prop2",
            kind="behaviour",
            lang="en",
            tags=frozenset({"eval-group"}),
            fixture="",
            raw={
                "id": "prop2",
                "query": "hello",
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "expect": {},
            },
            source="t.yaml",
        )
        with mock.patch.object(llm, "_build_adapter", lambda provider: StubAdapter()):
            result = case_runner.DefaultCaseRunner()(gcase, ARM0, {})
        self.assertNotIn("proposed", result)


class ProposedReportTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.cases("x", [case("a", "t1")])

    def runner(self, *, passed, answer):
        def run(gcase, arm, overrides):
            result = group_usage.new_result(gcase.id, arm, gcase.lang)
            result["passed"], result["answer"] = passed, answer
            result["checks"] = [{"name": "c", "ok": passed, "detail": "d"}]
            result["proposed"] = [{"tool": "create_task_plan", "args": '{"tasks": []}'}]
            return result

        return run

    def test_a_failed_row_says_what_was_proposed(self):
        out, _ = self.run_command(
            "--tag",
            "t1",
            "--arms",
            "t1",
            "--cost-cap-usd",
            "5",
            runner=self.runner(passed=False, answer=""),
        )
        self.assertIn('proposed create_task_plan({"tasks": []})', out)

    def test_a_row_that_passed_with_an_answer_stays_as_it_was(self):
        out, _ = self.run_command(
            "--tag",
            "t1",
            "--arms",
            "t1",
            "--cost-cap-usd",
            "5",
            runner=self.runner(passed=True, answer="ok"),
        )
        self.assertNotIn("proposed create_task_plan", out)

    def test_the_jsonl_row_keeps_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "run.jsonl"
            self.run_command(
                "--tag", "t1", "--arms", "t1", "--cost-cap-usd", "5", "--json", str(path),
                runner=self.runner(passed=False, answer=""),
            )  # fmt: skip
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(all(r["proposed"][0]["tool"] == "create_task_plan" for r in rows))


# --------------------------------------------------------------------------- #
# 3. The environment                                                          #
# --------------------------------------------------------------------------- #


def host_allowed(host="testserver"):
    request = HttpRequest()
    request.META["HTTP_HOST"] = host
    request.META["SERVER_NAME"] = host
    request.META["SERVER_PORT"] = "80"
    try:
        request.get_host()
        return True
    except Exception:  # noqa: BLE001 - DisallowedHost
        return False


class AllowedHostsTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.cases("x", [case("a", "t1")])

    @override_settings(ALLOWED_HOSTS=["localhost"], DEBUG=False)
    def test_the_helper_adds_testserver_and_puts_the_list_back(self):
        self.assertFalse(host_allowed())
        with harness.allow_test_host():
            self.assertTrue(host_allowed())
            self.assertIn("localhost", settings.ALLOWED_HOSTS)
        self.assertFalse(host_allowed())
        self.assertEqual(settings.ALLOWED_HOSTS, ["localhost"])

    @override_settings(ALLOWED_HOSTS=["localhost"], DEBUG=False)
    def test_a_real_run_can_serve_the_test_client_host_and_restores_the_list(self):
        seen = []

        def run(gcase, arm, overrides):
            seen.append(host_allowed())
            return flag_runner()(gcase, arm, overrides)

        self.run_command("--tag", "t1", "--arms", "t1", "--cost-cap-usd", "5", runner=run)
        self.assertEqual(seen, [True, True])  # arm0 and t1
        self.assertEqual(settings.ALLOWED_HOSTS, ["localhost"])

    @override_settings(ALLOWED_HOSTS=["localhost"], DEBUG=False)
    def test_the_list_is_restored_when_the_runner_crashes(self):
        def crash(gcase, arm, overrides):
            raise RuntimeError("boom")

        self.run_command("--tag", "t1", "--arms", "t1", "--cost-cap-usd", "5", runner=crash)
        self.assertEqual(settings.ALLOWED_HOSTS, ["localhost"])


class FakeIndices:
    def __init__(self, *, alias_to=None, plain=None, mappings=None, raises=None):
        self.alias_to = alias_to  # {alias: [index, ...]}
        self.plain = set(plain or ())
        self.mappings = mappings or {}
        self.raises = raises

    def exists_alias(self, name):
        if self.raises:
            raise self.raises
        return name in (self.alias_to or {})

    def exists(self, index):
        return index in self.plain

    def get_mapping(self, index):
        names = (self.alias_to or {}).get(index) or [index]
        return {n: {"mappings": {"properties": self.mappings.get(n, {})}} for n in names}


class FakeClient:
    def __init__(self, **kw):
        self.indices = FakeIndices(**kw)


VECTOR = {"embedding": {"type": "knn_vector", "dimension": 768}}


class PreflightTests(SimpleTestCase):
    def check(self, **kw):
        return harness.opensearch_preflight(FakeClient(**kw), alias="chunks_alias")

    def test_an_alias_over_a_vector_index_is_fine(self):
        self.assertEqual(
            self.check(alias_to={"chunks_alias": ["chunks_v1"]}, mappings={"chunks_v1": VECTOR}), []
        )

    def test_a_plain_index_is_not_an_alias(self):
        problems = self.check(plain={"chunks_alias"}, mappings={"chunks_alias": VECTOR})
        self.assertEqual(len(problems), 1)
        self.assertIn("plain index, not an alias", problems[0])
        self.assertIn("OPENSEARCH_ALIAS", problems[0])

    def test_the_dev_default_has_no_vector_mapping(self):
        problems = self.check(
            plain={"chunks_alias"}, mappings={"chunks_alias": {"title": {"type": "text"}}}
        )
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("not an alias" in p for p in problems))
        self.assertTrue(any("knn_vector" in p for p in problems))

    def test_an_alias_whose_index_maps_embedding_as_something_else(self):
        problems = self.check(
            alias_to={"chunks_alias": ["chunks_v1"]},
            mappings={"chunks_v1": {"embedding": {"type": "float"}}},
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("'float'", problems[0])
        self.assertIn("chunks_v1", problems[0])

    def test_every_index_behind_the_alias_is_checked(self):
        problems = self.check(
            alias_to={"chunks_alias": ["a", "b"]}, mappings={"a": VECTOR, "b": {}}
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("'b'", problems[0])

    def test_nothing_by_that_name(self):
        problems = self.check()
        self.assertEqual(len(problems), 1)
        self.assertIn("no alias or index", problems[0])

    def test_an_unreachable_cluster_is_a_problem_not_a_crash(self):
        problems = self.check(raises=ConnectionError("refused"))
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot read OpenSearch", problems[0])
        self.assertIn("refused", problems[0])

    def test_the_default_alias_comes_from_settings(self):
        seen = []

        class Spy(FakeClient):
            def __init__(self):
                super().__init__(alias_to={"from_settings": ["i"]}, mappings={"i": VECTOR})
                orig = self.indices.exists_alias
                self.indices.exists_alias = lambda name: seen.append(name) or orig(name)

        with mock.patch.dict(settings.SEARCH_ENGINE, {"OPENSEARCH_ALIAS": "from_settings"}):
            self.assertEqual(harness.opensearch_preflight(Spy()), [])
        self.assertEqual(seen, ["from_settings"])


class PreflightCommandTests(Workspace):
    """The command refuses a REAL run with the default runner, before any model call."""

    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.cases(
            "x",
            [
                case("beh", "t1"),
                case("rou", "t1", kind="routing", ok=["list_tasks"]),
            ],
        )

    def run_default(self, *argv, problems=None, **kw):
        from origin.search_engine.management.commands import agent_eval_group

        # The default runner is what triggers the check; stub its work, not the class.
        calls = []

        def fake_call(self_, gcase, arm, overrides):
            calls.append(gcase.id)
            return flag_runner()(gcase, arm, overrides)

        with (
            mock.patch.object(case_runner.DefaultCaseRunner, "__call__", fake_call),
            mock.patch.object(
                agent_eval_group.Command, "preflight", staticmethod(lambda: list(problems or []))
            ),
        ):
            out = self.run_command(*argv, **kw)
        return out, calls

    def test_a_bad_alias_refuses_before_any_call(self):
        with self.assertRaises(CommandError) as caught:
            self.run_default(
                "--tag",
                "t1",
                "--cost-cap-usd",
                "5",
                problems=["'x' is a plain index, not an alias"],
            )
        self.assertIn("PREFLIGHT failed, nothing was run", str(caught.exception))
        self.assertIn("plain index", str(caught.exception))
        self.assertEqual(self.stub.calls, 0)

    def test_a_good_alias_runs(self):
        (_out, _err), calls = self.run_default("--tag", "t1", "--cost-cap-usd", "5")
        self.assertIn("beh", calls)

    def test_a_routing_only_selection_needs_no_index(self):
        (_out, _err), calls = self.run_default(
            "--case", "rou", "--arms", "t1", "--cost-cap-usd", "5", problems=["broken"]
        )
        self.assertEqual(set(calls), {"rou"})  # arm0 and t1; the behaviour case never ran

    def test_the_dry_run_never_calls_it_and_points_at_preflight_only(self):
        (out, _err), _ = self.run_default("--dry-run", "--tag", "t1", problems=["broken"])
        self.assertIn("--preflight-only", out)

    def test_preflight_only_prints_the_problems_and_exits_1(self):
        (out, err), _ = self.run_default(
            "--tag", "t1", "--preflight-only", problems=["broken"], expect_exit=1
        )
        self.assertIn("PREFLIGHT: broken", err)

    def test_preflight_only_ok(self):
        (out, _err), _ = self.run_default("--tag", "t1", "--preflight-only")
        self.assertIn("preflight ok", out)

    def test_an_injected_runner_is_not_asked(self):
        from origin.search_engine.management.commands import agent_eval_group

        boom = mock.Mock(side_effect=AssertionError("must not run"))
        with mock.patch.object(agent_eval_group.Command, "preflight", staticmethod(boom)):
            self.run_command("--tag", "t1", "--cost-cap-usd", "5", runner=flag_runner())
        boom.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. expect_baseline                                                          #
# --------------------------------------------------------------------------- #


class ExpectBaselineLoadingTests(Workspace):
    def load(self, entries):
        self.cases("x", entries)
        return {
            c.id: c
            for c in harness.load_cases(
                group_dir=self.group, evals_dir=self.evals, include_legacy=False
            )
        }

    def test_a_string_is_the_baseline_and_a_mapping_names_arms(self):
        loaded = self.load(
            [
                case("a", "t1", expect_baseline="fail"),
                case("b", "t1", expect_baseline={"arm0": "FAIL", "t1": "pass"}),
                case("c", "t1"),
            ]
        )
        self.assertEqual(loaded["a"].expected, {ARM0: "fail"})
        self.assertEqual(loaded["b"].expected, {ARM0: "fail", "t1": "pass"})
        self.assertEqual(loaded["c"].expected, {})

    def test_anything_but_pass_or_fail_is_refused(self):
        with self.assertRaisesMessage(harness.HarnessError, "expect_baseline"):
            self.load([case("a", "t1", expect_baseline="maybe")])

    def test_the_key_never_reaches_the_behaviour_runner(self):
        self.assertIn("expect_baseline", harness.GROUP_ONLY_KEYS)
        loaded = self.load([case("a", "t1", expect_baseline="fail")])
        self.assertNotIn("expect_baseline", case_runner.prepare_case(loaded["a"]))


class ExpectBaselineReportTests(Workspace):
    def setUp(self):
        super().setUp()
        self.arm("t1", {FLAG_A: False})
        self.cases(
            "x",
            [
                case("fails-by-design", "t1", expect_baseline="fail"),
                case("control", "t1", expect_baseline="pass"),
                case("plain", "t1"),
            ],
        )

    def runner(self, verdicts):
        """`verdicts`: {(case, arm): passed}; default pass."""

        def run(gcase, arm, overrides):
            result = group_usage.new_result(gcase.id, arm, gcase.lang)
            result["passed"] = verdicts.get((gcase.id, arm), True)
            result["checks"] = [{"name": "c", "ok": result["passed"], "detail": "d"}]
            return result

        return run

    def go(self, verdicts, *extra):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "run.jsonl"
            out, _ = self.run_command(
                "--tag", "t1", "--arms", "t1", "--cost-cap-usd", "5", "--json", str(path), *extra,
                runner=self.runner(verdicts),
            )  # fmt: skip
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            summary = json.loads(path.with_suffix(".summary.json").read_text())
        return out, rows, summary

    def row(self, out, cid):
        (line,) = [ln for ln in out.splitlines() if ln.startswith(f"| {cid} | en |")]
        return [c.strip() for c in line.strip("|").split("|")]

    def test_a_baseline_that_fails_by_design_reads_as_expected(self):
        out, rows, _ = self.go({("fails-by-design", ARM0): False})
        self.assertEqual(self.row(out, "fails-by-design")[2:], ["FAIL (expected)", "PASS"])
        self.assertEqual(self.row(out, "control")[2:], ["PASS (expected)", "PASS"])
        self.assertEqual(self.row(out, "plain")[2:], ["PASS", "PASS"])
        self.assertIn("Expected results (`expect_baseline`): 2 of 2 as expected.", out)
        by = {(r["case_id"], r["arm"]): r for r in rows}
        self.assertEqual(by[("fails-by-design", ARM0)]["expected"], "fail")
        self.assertNotIn("expected", by[("plain", ARM0)])
        self.assertNotIn("expected", by[("fails-by-design", "t1")])

    def test_a_surprise_is_named(self):
        # the by-design failure passed, and the control failed
        out, _, _ = self.go({("control", ARM0): False})
        self.assertEqual(self.row(out, "fails-by-design")[2], "PASS (expected FAIL)")
        self.assertEqual(self.row(out, "control")[2], "FAIL (expected PASS)")
        self.assertIn("DIFFERENT", out)
        self.assertIn("fails-by-design on arm0: expected FAIL, got PASS", out)
        self.assertIn("control on arm0: expected PASS, got FAIL", out)

    def test_nothing_else_moves(self):
        # The expectation changes no verdict, pass count or regression arithmetic.
        _, rows, summary = self.go({("fails-by-design", ARM0): False})
        self.assertEqual(summary["arms"]["arm0"]["passed"], 2)
        self.assertEqual(summary["arms"]["arm0"]["failed"], 1)
        self.assertEqual(summary["regressions"], {"t1": []})
        self.assertEqual(summary["improvements"], {"t1": ["fails-by-design"]})

    def test_repetitions_and_crashes(self):
        # 1 of 2 reps passing is neither expected FAIL nor expected PASS; a crash is never "expected"
        flip = iter([True, False])

        def mixed(gcase, arm, overrides):
            result = group_usage.new_result(gcase.id, arm, gcase.lang)
            if gcase.id == "control":
                raise RuntimeError("boom")
            result["passed"] = next(flip) if gcase.id == "fails-by-design" and arm == ARM0 else True
            return result

        out, _ = self.run_command(
            "--tag", "t1", "--arms", "t1", "--reps", "2", "--cost-cap-usd", "5", runner=mixed
        )
        self.assertEqual(self.row(out, "fails-by-design")[2], "1/2 (expected FAIL)")
        self.assertEqual(self.row(out, "control")[2], "ERR")

    def test_a_run_with_no_expectation_prints_no_expectation_line(self):
        self.cases("x", [case("plain", "t1")])
        out, _, _ = self.go({})
        self.assertNotIn("expect_baseline", out)
        self.assertEqual(self.row(out, "plain")[2:], ["PASS", "PASS"])


class ShippedCasesTests(SimpleTestCase):
    def test_every_positive_resume_case_is_marked_as_failing_on_arm0(self):
        """`polarity: pos` is the case-shape test's promise that arm0 fails the prompt check
        (`test_eval_group_resume_runner`); the files must say so out loud for the report."""
        cases_ = [c for c in harness.load_cases() if c.kind == "resume"]
        self.assertTrue(cases_)
        wrong = [
            c.id
            for c in cases_
            if (c.raw.get("polarity") == "pos") != (c.expected.get(ARM0) == "fail")
        ]
        self.assertEqual(wrong, [])

    def test_a_negative_resume_case_is_not_marked(self):
        for c in harness.load_cases():
            if c.raw.get("polarity") == "neg":
                self.assertEqual(c.expected, {}, c.id)

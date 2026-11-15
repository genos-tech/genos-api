"""`documented_red` retrieval cases (MIP-1537): known-red cases stay visible but do not gate.

Five retrieval cases were red in 88-99 of the 99 successful `agent-evals` runs of 2026-11-02..14, so
the pass count carried them as permanent failures. A case with `documented_red: "<evidence>"` in
`retrieval_cases.yaml` is still run and shown, but it cannot fail `agent_eval`, and the summary prints
a second pair of lines (`Gated ...`) without it, which is all `scripts/retrieval_gate.py` reads.

Pinned here, with no model and no database (the runner is stubbed):
  * the exact set of marked cases, each with a non-empty evidence string, so marking one more is a
    visible diff in a test and not a quiet edit of a YAML file;
  * the command: a red marked case does not fail the run, a red normal case still does, the gated
    lines leave the marked cases out, the all-cases lines are unchanged, a marked case that passes is
    called out;
  * the harness output feeds the gate and the gate reads the gated numbers.
"""

import importlib.util
import io
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from origin.search_engine.agent.evals.runner import RETRIEVAL_CASES_PATH, CaseResult, load_cases

COMMAND = "origin.search_engine.management.commands.agent_eval"
ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "retrieval_gate", ROOT / "scripts" / "retrieval_gate.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# The five cases red in 88-99 of 99 runs. Adding to this set needs the evidence in the case itself
# AND a deliberate edit here; removing one (it was fixed) raises the gated numbers' meaning, so the
# floors in scripts/retrieval_gate.py should be re-derived then.
DOCUMENTED_RED = {
    "graph_native_crosslingual_vector_entry",
    "gibberish_returns_few_or_nothing",
    "scope_website_redesign_top",
    "mention_todo_boost",
    "mention_entity_boost_control",
}


class YamlTests(SimpleTestCase):
    def test_the_marked_set_is_exactly_the_five_with_evidence(self):
        cases = load_cases(RETRIEVAL_CASES_PATH)
        marked = {c["id"]: c["documented_red"] for c in cases if "documented_red" in c}
        self.assertEqual(set(marked), DOCUMENTED_RED)
        for case_id, why in marked.items():
            with self.subTest(case=case_id):
                self.assertIsInstance(why, str)
                self.assertIn(
                    " of the 99 successful agent-evals runs", why, "evidence, not a bare mark"
                )

    def test_enough_cases_stay_gated(self):
        cases = load_cases(RETRIEVAL_CASES_PATH)
        gated = [c for c in cases if not c.get("documented_red")]
        self.assertGreaterEqual(len(gated), gate.MIN_TOTAL)


def stub_case(case_id, passed=True, mrr=None):
    return CaseResult(
        case_id=case_id,
        passed=passed,
        duration_ms=1,
        failure_reasons=[] if passed else ["must_contain_title_in_top_n: missing ['x']"],
        metrics={} if mrr is None else {"mrr": mrr, "recall_at_n": mrr},
    )


class CommandTests(SimpleTestCase):
    """`agent_eval --retrieval` over three stub cases, run through the real command."""

    def run_command(self, cases, results):
        by_id = {r.case_id: r for r in results}
        out = io.StringIO()
        code = None
        with (
            mock.patch(f"{COMMAND}.load_cases", return_value=cases),
            mock.patch(f"{COMMAND}.run_retrieval_case", side_effect=lambda c: by_id[c["id"]]),
        ):
            try:
                call_command("agent_eval", "--retrieval", "--skip-env-check", stdout=out)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def cases(self):
        return [
            {"id": "ok_case", "query": "q"},
            {"id": "red_case", "query": "q", "documented_red": "red in 9 of 9 runs"},
            {"id": "other_case", "query": "q"},
        ]

    def test_a_red_marked_case_does_not_fail_the_run(self):
        code, out = self.run_command(
            self.cases(),
            [
                stub_case("ok_case", mrr=1.0),
                stub_case("red_case", passed=False, mrr=0.0),
                stub_case("other_case", mrr=1.0),
            ],
        )
        self.assertIsNone(code, out)
        self.assertRegex(out, r"RED\s+red_case")
        self.assertIn("documented red: red in 9 of 9 runs", out)

    def test_a_red_normal_case_still_fails_the_run(self):
        code, out = self.run_command(
            self.cases(),
            [
                stub_case("ok_case", mrr=1.0),
                stub_case("red_case", passed=False, mrr=0.0),
                stub_case("other_case", passed=False, mrr=0.0),
            ],
        )
        self.assertEqual(code, 1, out)

    def test_the_gated_lines_leave_the_marked_cases_out_and_the_old_lines_stay(self):
        _, out = self.run_command(
            self.cases(),
            [
                stub_case("ok_case", mrr=1.0),
                stub_case("red_case", passed=False, mrr=0.0),
                stub_case("other_case", mrr=0.5),
            ],
        )
        # unchanged meaning: every case
        self.assertIn("2/3 passed.", out)
        self.assertIn("Continuous metrics: mrr=0.500 (n=3)  recall_at_n=0.500 (n=3)", out)
        self.assertIn("- red_case  (documented red)", out)
        # the gated population: the marked case is out of the count and of the means
        self.assertIn("Gated (documented-red cases excluded): 2/2 passed.", out)
        self.assertIn("Gated continuous metrics: mrr=0.750 (n=2)  recall_at_n=0.750 (n=2)", out)
        self.assertIn("Documented red: 1 case(s), 0 passing.", out)

    def test_a_marked_case_that_passes_is_called_out(self):
        _, out = self.run_command(
            self.cases(),
            [stub_case("ok_case"), stub_case("red_case"), stub_case("other_case")],
        )
        self.assertRegex(out, r"GRN!\s+red_case")
        self.assertIn("IT PASSED: remove `documented_red`", out)
        self.assertIn("Documented red: 1 case(s), 1 passing (red_case: remove the mark).", out)

    def test_with_no_marked_case_the_gated_lines_equal_the_all_cases_lines(self):
        cases = [{"id": "a", "query": "q"}, {"id": "b", "query": "q"}]
        code, out = self.run_command(cases, [stub_case("a", mrr=1.0), stub_case("b", mrr=0.5)])
        self.assertIsNone(code)
        self.assertIn("Gated (documented-red cases excluded): 2/2 passed.", out)
        self.assertIn("Gated continuous metrics: mrr=0.750 (n=2)  recall_at_n=0.750 (n=2)", out)
        self.assertNotIn("Documented red:", out)

    def test_the_gate_reads_the_gated_numbers_of_this_output(self):
        # one red marked case with mrr 0 would drag the all-cases mean down; the gate must not see it
        _, out = self.run_command(
            self.cases(),
            [
                stub_case("ok_case", mrr=1.0),
                stub_case("red_case", passed=False, mrr=0.0),
                stub_case("other_case", mrr=1.0),
            ],
        )
        passed, total, mrr, recall = gate.parse(out)
        self.assertEqual((passed, total, mrr, recall), (2, 2, 1.0, 1.0))

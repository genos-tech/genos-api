"""The floors of the `agent-evals` retrieval gate (MIP-1527): `scripts/retrieval_gate.py`.

The workflow pipes `agent_eval --retrieval` into `eval.out` and the script decides from it. Pinned
here: the floors (so a change of one is a visible, deliberate diff), the boundary on each of the
three, that the GATED lines are read (the cases marked `documented_red` are left out of them, MIP-1537)
and not the all-cases lines or a per-case line, and that an output the script cannot read FAILS (a
broken harness, or one from before the gated lines, must not look like a pass).
"""

import importlib.util
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "retrieval_gate", ROOT / "scripts" / "retrieval_gate.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def eval_output(passed=66, total=70, mrr=0.830, recall=0.900, red=5):
    """The shape `agent_eval --retrieval` prints (agent_eval.py `_print_summary`): the all-cases
    lines first (they must NOT be what the gate reads), then the gated lines."""
    all_total = total + red
    return (
        "[retrieval] title_exact_foo ... PASS\n"
        "        metrics: mrr=1.000 recall_at_n=1.000\n"
        "[retrieval] mention_todo_boost ... RED\n"
        "        metrics: mrr=0.000 recall_at_n=0.000\n"
        "\n"
        f"{passed}/{all_total} passed.\n"
        "Failures:\n  - mention_todo_boost  (documented red)\n"
        "\nContinuous metrics: mrr=0.100 (n=53)  recall_at_n=0.100 (n=53)\n"
        f"\nGated (documented-red cases excluded): {passed}/{total} passed.\n"
        f"Gated continuous metrics: mrr={mrr:.3f} (n=48)  recall_at_n={recall:.3f} (n=48)\n"
        f"Documented red: {red} case(s), 0 passing.\n"
    )


class FloorsTests(TestCase):
    def test_the_floors_are_the_measured_ones(self):
        # observed minimum on the 99 successful runs 2026-11-02..14, five documented-red cases left
        # out: 62 of 70 / mrr 0.822 / recall 0.875 (MIP-1537; the derivation is in the module docstring)
        self.assertEqual(gate.MIN_PASS, 58)
        self.assertEqual(gate.MIN_MRR, 0.78)
        self.assertEqual(gate.MIN_RECALL, 0.84)
        self.assertEqual(gate.MIN_TOTAL, 70)

    def test_the_observed_range_passes(self):
        for passed, mrr, recall in ((62, 0.822, 0.875), (70, 0.921, 1.0), (66, 0.864, 0.939)):
            ok, line = gate.evaluate(eval_output(passed=passed, mrr=mrr, recall=recall))
            self.assertTrue(ok, line)

    def test_each_floor_trips_on_its_own(self):
        ok, line = gate.evaluate(eval_output(passed=57))
        self.assertFalse(ok)
        self.assertIn("57/70", line)
        self.assertIn("FAIL: regression", line)
        self.assertFalse(gate.evaluate(eval_output(mrr=0.779))[0])
        self.assertFalse(gate.evaluate(eval_output(recall=0.839))[0])

    def test_each_floor_is_inclusive(self):
        self.assertTrue(gate.evaluate(eval_output(passed=58, mrr=0.78, recall=0.84))[0])

    def test_the_old_floors_would_have_missed_these(self):
        # a dozen lost cases and a lane at 0.7: red now, green under 45 / 0.65 / 0.65
        self.assertFalse(gate.evaluate(eval_output(passed=50, mrr=0.70, recall=0.72))[0])
        # mrr 0.77 and recall 0.82 passed the #803 floors (0.75 / 0.80); they trip the new ones
        self.assertFalse(gate.evaluate(eval_output(mrr=0.77, recall=0.82))[0])

    def test_marking_too_many_cases_red_cannot_empty_the_gate(self):
        ok, line = gate.evaluate(eval_output(passed=60, total=62, red=13))
        self.assertFalse(ok)
        self.assertIn("only 62 gated cases", line)

    def test_a_dead_vector_lane_still_trips(self):
        self.assertFalse(gate.evaluate(eval_output(passed=30, mrr=0.05, recall=0.10))[0])


class ParseTests(TestCase):
    def test_the_gated_lines_are_read_not_the_all_cases_or_a_per_case_line(self):
        # per-case lines say mrr=1.000 / 0.000 and the all-cases lines say 0.100: only the gated
        # lines decide
        passed, total, mrr, recall = gate.parse(eval_output(mrr=0.812, recall=0.874))
        self.assertEqual((passed, total, mrr, recall), (66, 70, 0.812, 0.874))

    def test_an_output_without_the_gated_lines_fails(self):
        # what the harness printed before MIP-1537: all-cases lines only, which must not be
        # compared with floors derived on the gated population
        old = (
            "66/75 passed.\nFailures:\n  - x\n"
            "\nContinuous metrics: mrr=0.830 (n=51)  recall_at_n=0.900 (n=51)\n"
        )
        ok, line = gate.evaluate(old)
        self.assertFalse(ok)
        self.assertIn("cannot read", line)

    def test_every_unreadable_output_fails(self):
        for bad in (
            "",
            "Traceback (most recent call last):\n  File ...",
            eval_output().replace("66/70 passed.", "done."),
            eval_output().replace("Gated continuous metrics:", "Other:"),
            eval_output().replace("mrr=0.830 (n=48)  ", ""),
            eval_output().replace("recall_at_n=0.900 (n=48)", ""),
        ):
            with self.subTest(bad=bad[:40]):
                ok, line = gate.evaluate(bad)
                self.assertFalse(ok)
                self.assertIn("cannot read", line)

    def test_a_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(gate.main(["retrieval_gate.py", str(Path(tmp) / "nope.out")]), 1)

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp) / "good.out", Path(tmp) / "bad.out"
            good.write_text(eval_output())
            bad.write_text(eval_output(passed=40))
            self.assertEqual(gate.main(["retrieval_gate.py", str(good)]), 0)
            self.assertEqual(gate.main(["retrieval_gate.py", str(bad)]), 1)

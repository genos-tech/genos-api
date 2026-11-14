"""The floors of the `agent-evals` retrieval gate (MIP-1527): `scripts/retrieval_gate.py`.

The workflow pipes `agent_eval --retrieval` into `eval.out` and the script decides from it. Pinned
here: the floors (so a change of one is a visible, deliberate diff), the boundary on each of the
three, that the AGGREGATE line is read and not a per-case line, and that an output the script
cannot read FAILS (a broken harness must not look like a pass).
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


def eval_output(passed=66, total=75, mrr=0.830, recall=0.900):
    """The shape `agent_eval --retrieval` prints (agent_eval.py `_print_summary`)."""
    return (
        "[retrieval] title_exact_foo ... PASS\n"
        "        metrics: mrr=1.000 recall_at_n=1.000\n"
        "[retrieval] mention_todo_boost ... FAIL\n"
        "        metrics: mrr=0.000 recall_at_n=0.000\n"
        "\n"
        f"{passed}/{total} passed.\n"
        "Failures:\n  - mention_todo_boost\n"
        f"\nContinuous metrics: mrr={mrr:.3f} (n={total})  recall_at_n={recall:.3f} (n={total})\n"
    )


class FloorsTests(TestCase):
    def test_the_floors_are_the_audited_ones(self):
        # observed minimum on 13 PR runs: 62 / 0.796 / 0.833 (audit 2026-11-14, MIP-1527)
        self.assertEqual(gate.MIN_PASS, 58)
        self.assertEqual(gate.MIN_MRR, 0.75)
        self.assertEqual(gate.MIN_RECALL, 0.80)

    def test_the_observed_range_passes(self):
        for passed, mrr, recall in ((62, 0.796, 0.833), (69, 0.862, 0.931), (66, 0.83, 0.9)):
            ok, line = gate.evaluate(eval_output(passed=passed, mrr=mrr, recall=recall))
            self.assertTrue(ok, line)

    def test_each_floor_trips_on_its_own(self):
        ok, line = gate.evaluate(eval_output(passed=57))
        self.assertFalse(ok)
        self.assertIn("57/75", line)
        self.assertIn("FAIL: regression", line)
        self.assertFalse(gate.evaluate(eval_output(mrr=0.749))[0])
        self.assertFalse(gate.evaluate(eval_output(recall=0.799))[0])

    def test_each_floor_is_inclusive(self):
        self.assertTrue(gate.evaluate(eval_output(passed=58, mrr=0.75, recall=0.80))[0])

    def test_the_old_floors_would_have_missed_these(self):
        # a dozen lost cases and a lane at 0.7: red now, green under 45 / 0.65 / 0.65
        self.assertFalse(gate.evaluate(eval_output(passed=50, mrr=0.70, recall=0.72))[0])

    def test_a_dead_vector_lane_still_trips(self):
        self.assertFalse(gate.evaluate(eval_output(passed=30, mrr=0.05, recall=0.10))[0])


class ParseTests(TestCase):
    def test_the_aggregate_line_is_read_not_a_per_case_line(self):
        # per-case lines say mrr=1.000 / 0.000; only the aggregate decides
        passed, total, mrr, recall = gate.parse(eval_output(mrr=0.812, recall=0.874))
        self.assertEqual((passed, total, mrr, recall), (66, 75, 0.812, 0.874))

    def test_every_unreadable_output_fails(self):
        for bad in (
            "",
            "Traceback (most recent call last):\n  File ...",
            eval_output().replace("66/75 passed.", "done."),
            eval_output().replace("Continuous metrics:", "Other:"),
            eval_output().replace("mrr=0.830 (n=75)  ", ""),
            eval_output().replace("recall_at_n=0.900 (n=75)", ""),
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

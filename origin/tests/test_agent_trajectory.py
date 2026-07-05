"""Unit tests for the trajectory capture/diff logic (§5.1).

Pure-function coverage only — `tool_set`, baseline round-trip, the
tolerant set diff, and the markdown rendering. The LLM-running path
(`agent_eval_trajectory` executing the behavior suite) is exercised by
the CI workflow itself, same as `agent_eval`. No DB, no LLM.
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.trajectory import (
    BASELINE_VERSION,
    diff_trajectories,
    dump_baseline,
    format_diff,
    load_baseline,
    tool_set,
)


def _result(*tool_names):
    return SimpleNamespace(tool_results=[{"tool_name": n} for n in tool_names])


class ToolSetTests(SimpleTestCase):
    def test_collapses_repeats_and_sorts(self):
        """Repeat calls are the model-variance we tolerate by design."""
        result = _result("search_kb", "list_tasks", "search_kb", "list_tasks", "search_kb")
        self.assertEqual(tool_set(result), ["list_tasks", "search_kb"])

    def test_handles_no_tools_and_malformed_traces(self):
        self.assertEqual(tool_set(SimpleNamespace(tool_results=[])), [])
        self.assertEqual(tool_set(SimpleNamespace(tool_results=None)), [])
        self.assertEqual(tool_set(SimpleNamespace(tool_results=[{"arguments": {}}])), [])


class BaselineRoundTripTests(SimpleTestCase):
    def test_dump_then_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trajectory_baseline.json"
            dump_baseline({"b_case": ["z_tool", "a_tool"], "a_case": []}, path)
            loaded = load_baseline(path)
        self.assertEqual(loaded, {"a_case": [], "b_case": ["a_tool", "z_tool"]})

    def test_missing_file_is_none_not_error(self):
        """Supported bootstrap state: the feature lands before the first
        baseline is committed."""
        self.assertIsNone(load_baseline(Path("/nonexistent/trajectory_baseline.json")))

    def test_version_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trajectory_baseline.json"
            path.write_text(json.dumps({"version": BASELINE_VERSION + 1, "cases": {}}))
            with self.assertRaises(ValueError):
                load_baseline(path)


class DiffTests(SimpleTestCase):
    def test_clean_when_identical(self):
        base = {"a": ["t1", "t2"], "b": []}
        diff = diff_trajectories(base, {"a": ["t2", "t1"], "b": []})
        self.assertTrue(diff.is_clean)
        self.assertEqual(diff.unchanged, 2)

    def test_reports_added_removed_new_missing(self):
        baseline = {"a": ["t1", "t2"], "gone": ["t9"], "same": ["s"]}
        current = {"a": ["t1", "t3"], "brand_new": ["n"], "same": ["s"]}
        diff = diff_trajectories(baseline, current)
        self.assertEqual(diff.changed, {"a": (["t3"], ["t2"])})
        self.assertEqual(diff.new_cases, ["brand_new"])
        self.assertEqual(diff.missing_cases, ["gone"])
        self.assertEqual(diff.unchanged, 1)
        self.assertFalse(diff.is_clean)

    def test_format_diff_renders_the_review_table(self):
        diff = diff_trajectories({"a": ["t1"]}, {"a": ["t1", "t2"]})
        text = format_diff(diff)
        self.assertIn("report-only", text)
        self.assertIn("| `a` | +t2 | — |", text)
        self.assertIn("--write-baseline", text)

    def test_format_diff_clean_run_has_no_table(self):
        text = format_diff(diff_trajectories({"a": ["t"]}, {"a": ["t"]}))
        self.assertIn("changed: **0**", text)
        self.assertNotIn("| case |", text)

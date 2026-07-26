"""`ai_credit_benchmark` — the matrix runner and the token alarm.

Everything here MOCKS `run_behavior_case`: the command's job is
orchestration and arithmetic, and its live-provider nature (every cell
spends real money) is exactly why the tests must never reach a
provider. What must hold:

  1. The meter gate: with AI_COST_METER off the command refuses — the
     ledger is its data source, and ¥0 cells would be confident
     nonsense.
  2. Failures are kept out of the percentiles but reported: a failed
     cell's cost is real, but it is not a "typical request".
  3. The regression alarm compares TOKENS and exits non-zero past the
     threshold — a rate-card change cannot fire it.
"""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.conf import settings as dj_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from origin.search_engine.management.commands import ai_credit_benchmark as bench


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))

_CASES = [
    {"id": "case_a", "query": "a"},
    {"id": "case_b", "query": "b"},
    {"id": "case_c", "query": "c"},
]


def _result(tokens=1000, jpy_milli=15_000, error=""):
    return SimpleNamespace(total_tokens=tokens, cost_jpy_milli=jpy_milli, error=error)


def _run(*args, results=None, **opts) -> str:
    out = StringIO()
    with (
        mock.patch.object(bench.Command, "_load_cases", return_value=list(_CASES)),
        mock.patch.object(
            bench, "run_behavior_case", side_effect=results or (lambda case: _result())
        ),
    ):
        call_command("ai_credit_benchmark", *args, stdout=out, **opts)
    return out.getvalue()


class MeterGateTests(SimpleTestCase):
    def test_meter_off_refuses(self):
        with override_settings(SEARCH_ENGINE=_se(AI_COST_METER=False)):
            with self.assertRaises(CommandError) as cm:
                call_command("ai_credit_benchmark")
        self.assertIn("AI_COST_METER", str(cm.exception))


class MatrixTests(SimpleTestCase):
    @METER_ON
    def test_reports_per_cell_and_allowances(self):
        out = _run(providers="gemini", efforts="medium")
        self.assertIn("gemini / medium", out)
        self.assertIn("tokens/case", out)
        # ¥15/case at ¥15/credit = 1cr mean; pro 100cr ≈ 100 requests.
        self.assertIn("pro", out)
        self.assertIn("100 request(s)/month", out)
        self.assertIn("every cell is a live provider run", out)

    @METER_ON
    def test_the_choice_is_bound_per_cell_and_reset(self):
        seen = []

        def spy(case):
            from origin.search_engine.llm.choice import get_llm_choice  # noqa: PLC0415

            seen.append(get_llm_choice())
            return _result()

        _run(providers="gemini", efforts="low,high", results=spy)
        self.assertEqual(len(seen), 6, "3 cases × 2 efforts")
        self.assertTrue(all(c is not None and c.provider == "gemini" for c in seen))
        self.assertEqual({c.effort for c in seen}, {"low", "high"})
        from origin.search_engine.llm.choice import get_llm_choice  # noqa: PLC0415

        self.assertIsNone(get_llm_choice(), "the binding must not leak out of the run")

    @METER_ON
    def test_failed_cells_are_reported_not_averaged(self):
        calls = iter([_result(), _result(error="boom"), _result()])
        out = _run(providers="gemini", efforts="medium", results=lambda case: next(calls))
        self.assertIn("1 failed cell(s)", out)
        self.assertIn("not a typical request", out)

    @METER_ON
    def test_a_raising_case_does_not_kill_the_matrix(self):
        def flaky(case):
            if case["id"] == "case_b":
                raise RuntimeError("provider 500")
            return _result()

        out = _run(providers="gemini", efforts="medium", results=flaky)
        self.assertIn("1 failed cell(s)", out)

    @METER_ON
    def test_unknown_case_id_is_a_command_error(self):
        with mock.patch.object(bench.Command, "_run_cells", return_value=[]):
            with self.assertRaises(CommandError):
                call_command("ai_credit_benchmark", cases="no_such_case")


class BaselineTests(SimpleTestCase):
    def _tmp_baseline(self):
        from tempfile import NamedTemporaryFile  # noqa: PLC0415

        tmp = NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        from pathlib import Path  # noqa: PLC0415

        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        return mock.patch.object(bench, "BASELINE_PATH", type(bench.BASELINE_PATH)(tmp.name))

    @METER_ON
    def test_write_then_pass_within_threshold(self):
        with self._tmp_baseline():
            _run(write_baseline=True)
            out = _run(check_baseline=True)
        self.assertIn("within the regression threshold", out)

    @METER_ON
    def test_regression_exits_nonzero_in_tokens(self):
        with self._tmp_baseline():
            _run(write_baseline=True)  # 1000 tokens/case
            with self.assertRaises(SystemExit):
                _run(
                    check_baseline=True,
                    results=lambda case: _result(tokens=5000),  # +400%
                )

    @METER_ON
    def test_a_price_change_cannot_fire_the_alarm(self):
        """Same tokens, wildly different yen — the alarm must not care."""
        with self._tmp_baseline():
            _run(write_baseline=True, results=lambda case: _result(jpy_milli=15_000))
            out = _run(
                check_baseline=True,
                results=lambda case: _result(jpy_milli=999_000),  # 66× the yen
            )
        self.assertIn("within the regression threshold", out)

    @METER_ON
    def test_baseline_records_the_rate_card_for_context_only(self):
        with self._tmp_baseline():
            _run(write_baseline=True)
            data = json.loads(bench.BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertIn("rate_card_at_baseline", data)
        self.assertEqual(data["total_tokens"], 3000)

    @METER_ON
    def test_check_without_baseline_is_a_command_error(self):
        with self._tmp_baseline():
            bench.BASELINE_PATH.unlink(missing_ok=True)
            with self.assertRaises(CommandError):
                _run(check_baseline=True)

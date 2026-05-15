"""`python manage.py agent_eval` — run the agent eval harness.

Loads cases from `agent/evals/cases.yaml`, runs each through
`run_agent`, and prints a pass/fail summary. Exit code is 0 if every
case passed, 1 otherwise (CI-friendly).

Examples:

    # Run every case
    python manage.py agent_eval

    # Run just one case (fastest dev loop)
    python manage.py agent_eval --case prompt_injection_ignore_instructions

    # Stop on the first failure
    python manage.py agent_eval --fail-fast
"""

from __future__ import annotations

import sys
from typing import Any

from django.core.management.base import BaseCommand

from origin.search_engine.agent.evals.runner import (
    CASES_PATH,
    CaseResult,
    load_cases,
    run_case,
)


class Command(BaseCommand):
    help = "Run the agent evaluation harness against cases.yaml."

    def add_arguments(self, parser):
        parser.add_argument(
            "--case",
            dest="case_id",
            default=None,
            help="Run only the case with this id (matches `id:` in cases.yaml).",
        )
        parser.add_argument(
            "--fail-fast",
            action="store_true",
            help="Stop on the first failing case.",
        )

    def handle(self, *args, **options):
        case_id_filter: str | None = options.get("case_id")
        fail_fast: bool = options.get("fail_fast") or False

        try:
            cases = load_cases()
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR(f"cases.yaml not found at {CASES_PATH}"))
            sys.exit(2)
        except Exception as e:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"Failed to parse cases.yaml: {e}"))
            sys.exit(2)

        if case_id_filter:
            cases = [c for c in cases if c.get("id") == case_id_filter]
            if not cases:
                self.stderr.write(self.style.ERROR(f"No case found with id={case_id_filter!r}"))
                sys.exit(2)

        self.stdout.write(f"Running {len(cases)} case(s)...\n")

        results: list[CaseResult] = []
        for case in cases:
            result = run_case(case)
            results.append(result)
            self._print_one(result)
            if fail_fast and not result.passed:
                self.stdout.write(self.style.WARNING("\n--fail-fast: stopping.\n"))
                break

        self._print_summary(results)

        # Exit code: any failure → 1
        if any(not r.passed for r in results):
            sys.exit(1)

    def _print_one(self, r: CaseResult) -> None:
        label = self.style.SUCCESS("PASS") if r.passed else self.style.ERROR("FAIL")
        detail = f"({r.step_count} step{'s' if r.step_count != 1 else ''}, {r.duration_ms} ms)"
        self.stdout.write(f"  {label}  {r.case_id:<48} {detail}")
        for reason in r.failure_reasons:
            self.stdout.write(self.style.ERROR(f"        - {reason}"))

    def _print_summary(self, results: list[CaseResult]) -> None:
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        self.stdout.write("")
        if passed == total:
            self.stdout.write(self.style.SUCCESS(f"{passed}/{total} passed."))
        else:
            self.stdout.write(self.style.ERROR(f"{passed}/{total} passed."))
            failed = [r.case_id for r in results if not r.passed]
            self.stdout.write("Failures:")
            for cid in failed:
                self.stdout.write(f"  - {cid}")

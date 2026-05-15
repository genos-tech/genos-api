"""`python manage.py agent_eval` — run the agent eval harness.

Two suite types, sharing the same CLI shape:

  * **Behavior** (default) — full agent loop, real Gemini/Claude calls,
    asserts on the NDJSON event stream. Source: `agent/evals/cases.yaml`.

  * **Retrieval** (`--retrieval`) — direct `search(...)` calls, no LLM,
    asserts on the ranked entity list. Source:
    `agent/evals/retrieval_cases.yaml`.

Exit code is 0 if every case in the chosen suite(s) passed, 1
otherwise (CI-friendly).

Examples:

    python manage.py agent_eval                       # behavior suite
    python manage.py agent_eval --retrieval           # retrieval suite (fast, no LLM)
    python manage.py agent_eval --all                 # both suites
    python manage.py agent_eval --case <id>           # one case (auto-detects suite)
    python manage.py agent_eval --retrieval --fail-fast
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from origin.search_engine.agent.evals.runner import (
    BEHAVIOR_CASES_PATH,
    RETRIEVAL_CASES_PATH,
    CaseResult,
    load_cases,
    run_behavior_case,
    run_retrieval_case,
)


class Command(BaseCommand):
    help = "Run the agent evaluation harness (behavior and/or retrieval suite)."

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
        suite_group = parser.add_mutually_exclusive_group()
        suite_group.add_argument(
            "--retrieval",
            action="store_true",
            help="Run only the retrieval suite (fast, no LLM calls).",
        )
        suite_group.add_argument(
            "--all",
            dest="run_all",
            action="store_true",
            help="Run both behavior and retrieval suites.",
        )

    def handle(self, *args, **options):
        case_id_filter: str | None = options.get("case_id")
        fail_fast: bool = options.get("fail_fast") or False
        retrieval_only: bool = options.get("retrieval") or False
        run_all: bool = options.get("run_all") or False

        # Decide which suite(s) to run.
        if retrieval_only:
            suites = [("retrieval", RETRIEVAL_CASES_PATH, run_retrieval_case)]
        elif run_all:
            suites = [
                ("behavior", BEHAVIOR_CASES_PATH, run_behavior_case),
                ("retrieval", RETRIEVAL_CASES_PATH, run_retrieval_case),
            ]
        else:
            suites = [("behavior", BEHAVIOR_CASES_PATH, run_behavior_case)]

        all_results: list[CaseResult] = []
        any_failed = False

        for label, path, runner in suites:
            try:
                cases = load_cases(path)
            except FileNotFoundError:
                self.stderr.write(self.style.ERROR(f"{label} cases not found at {path}"))
                sys.exit(2)
            except Exception as e:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"Failed to parse {path}: {e}"))
                sys.exit(2)

            if case_id_filter:
                cases = [c for c in cases if c.get("id") == case_id_filter]
                if not cases:
                    # The case might live in a different suite; skip this one.
                    continue

            self.stdout.write(
                f"\n=== {label} suite ({len(cases)} case{'s' if len(cases) != 1 else ''}) ==="
            )
            for case in cases:
                result = runner(case)
                all_results.append(result)
                self._print_one(result)
                if not result.passed:
                    any_failed = True
                    if fail_fast:
                        self.stdout.write(self.style.WARNING("\n--fail-fast: stopping.\n"))
                        break
            else:
                # Inner loop completed without break — continue with the next suite.
                continue
            # Broke out of inner loop (fail-fast); stop running further suites too.
            break

        if case_id_filter and not all_results:
            self.stderr.write(self.style.ERROR(f"No case found with id={case_id_filter!r}"))
            sys.exit(2)

        self._print_summary(all_results)

        if any_failed:
            sys.exit(1)

    def _print_one(self, r: CaseResult) -> None:
        label = self.style.SUCCESS("PASS") if r.passed else self.style.ERROR("FAIL")
        if r.tool_call_count > 0 or r.step_count > 0:
            detail = f"({r.step_count} step{'s' if r.step_count != 1 else ''}, {r.duration_ms} ms)"
        else:
            detail = f"({r.duration_ms} ms)"
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

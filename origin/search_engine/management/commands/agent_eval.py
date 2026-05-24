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
    python manage.py agent_eval --judge               # behavior + LLM judge
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from django.core.management.base import BaseCommand

from origin.search_engine.agent.evals.judge import judge_answer
from origin.search_engine.agent.evals.runner import (
    BEHAVIOR_CASES_PATH,
    RETRIEVAL_CASES_PATH,
    CaseResult,
    load_cases,
    run_behavior_case,
    run_retrieval_case,
)

RUNS_DIR = Path(__file__).resolve().parents[3] / "agent" / "evals" / "runs"


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
        parser.add_argument(
            "--judge",
            action="store_true",
            help=(
                "Score each behavior case's answer with an LLM judge "
                "(faithfulness / citation_precision / completeness). "
                "Adds ~1 LLM call per case; results persisted to "
                "agent/evals/runs/<timestamp>.jsonl."
            ),
        )

    def handle(self, *args, **options):
        case_id_filter: str | None = options.get("case_id")
        fail_fast: bool = options.get("fail_fast") or False
        retrieval_only: bool = options.get("retrieval") or False
        run_all: bool = options.get("run_all") or False
        run_judge: bool = options.get("judge") or False

        if run_judge and retrieval_only:
            self.stderr.write(
                self.style.ERROR(
                    "--judge only applies to behavior cases (retrieval cases have no answer to judge)."
                )
            )
            sys.exit(2)

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
                if run_judge and label == "behavior" and result.answer:
                    result.judge_scores = judge_answer(
                        query=result.query,
                        sources=result.sources,
                        answer=result.answer,
                        tool_results=result.tool_results,
                    )
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

        self._print_summary(all_results, run_judge=run_judge)

        if run_judge:
            self._persist_judge_run(all_results)

        if any_failed:
            sys.exit(1)

    def _print_one(self, r: CaseResult) -> None:
        label = self.style.SUCCESS("PASS") if r.passed else self.style.ERROR("FAIL")
        ttft_part = f", ttft {r.ttft_ms} ms" if getattr(r, "ttft_ms", -1) >= 0 else ""
        if r.tool_call_count > 0 or r.step_count > 0:
            detail = (
                f"({r.step_count} step{'s' if r.step_count != 1 else ''}, "
                f"{r.duration_ms} ms{ttft_part})"
            )
        else:
            detail = f"({r.duration_ms} ms{ttft_part})"
        self.stdout.write(f"  {label}  {r.case_id:<48} {detail}")
        for reason in r.failure_reasons:
            self.stdout.write(self.style.ERROR(f"        - {reason}"))
        if r.judge_scores is not None:
            j = r.judge_scores
            note = j.get("error") or j.get("notes") or ""
            self.stdout.write(
                "        "
                f"judge: faith={j.get('faithfulness', 0):.2f} "
                f"cite={j.get('citation_precision', 0):.2f} "
                f"compl={j.get('completeness', 0):.2f}" + (f"  — {note}" if note else "")
            )

    def _print_summary(self, results: list[CaseResult], *, run_judge: bool) -> None:
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

        if run_judge:
            judged = [r for r in results if r.judge_scores is not None]
            if judged:

                def _avg(key: str) -> float:
                    vals = [r.judge_scores.get(key, 0.0) for r in judged]
                    return sum(vals) / len(vals)

                self.stdout.write(
                    self.style.NOTICE(
                        f"\nLLM judge ({len(judged)} cases): "
                        f"faith={_avg('faithfulness'):.2f}  "
                        f"cite={_avg('citation_precision'):.2f}  "
                        f"compl={_avg('completeness'):.2f}"
                    )
                )

    def _persist_judge_run(self, results: list[CaseResult]) -> None:
        """Append one JSONL line per case to `agent/evals/runs/<ts>.jsonl`.

        Includes the judge scores plus enough context (case id, query,
        answer, sources) to inspect or re-judge later. Useful for
        tracking quality trends across PRs.
        """
        judged = [r for r in results if r.judge_scores is not None]
        if not judged:
            return

        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            # Run from the repo root if we can find it. Suppress
            # stderr so "fatal: not a git repository" doesn't leak
            # into the eval output when running inside a container
            # whose CWD isn't a git checkout.
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                text=True,
                timeout=5,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:  # noqa: BLE001 — outside git or no git installed
            sha = "unknown"

        out_path = RUNS_DIR / f"{ts}-{sha}.jsonl"
        with out_path.open("w") as f:
            for r in judged:
                f.write(
                    json.dumps(
                        {
                            "case_id": r.case_id,
                            "passed": r.passed,
                            "duration_ms": r.duration_ms,
                            "ttft_ms": r.ttft_ms,
                            "query": r.query,
                            "answer": r.answer,
                            "sources": [
                                {
                                    "entity_id": s.get("entity_id"),
                                    "title": s.get("title"),
                                }
                                for s in r.sources
                            ],
                            "tool_results": [
                                {
                                    "tool_name": tr.get("tool_name"),
                                    "arguments": tr.get("arguments"),
                                    "result": tr.get("result"),
                                }
                                for tr in r.tool_results
                            ],
                            "judge": r.judge_scores,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        self.stdout.write(
            self.style.SUCCESS(f"\nWrote judge run to {out_path.relative_to(os.getcwd())}")
        )

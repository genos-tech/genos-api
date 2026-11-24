"""`python manage.py agent_eval_mcp_injection`: the prompt-injection cases for the MCP surface (MIP-2295).

    # what would it cost? NO model call, no database, no credentials
    python manage.py agent_eval_mcp_injection --dry-run

    # the billed run: needs the maintainer's go (--ken-approved) and a hard cap; never CI
    python manage.py agent_eval_mcp_injection --reps 3 --cost-cap-usd 5 --ken-approved --json run.jsonl

The driver and what it does and does not do: `agent/evals/group/mcp_injection.py`. Writes the model calls are never
run. The JSONL keeps every tool call and the answer of every repetition: Claude judges it with `group/injection_rubric.md`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine.agent.evals.group import mcp_injection as driver
from origin.search_engine.agent.evals.group import usage as group_usage


class Command(BaseCommand):
    help = "Prompt-injection cases for the MCP surface (skill text, HTML-note pages, tool results)."

    #: injectable for tests: `() -> anthropic-shaped client`
    client_factory = staticmethod(lambda: _anthropic_client())

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan and the cost estimate; call nothing.",
        )
        parser.add_argument(
            "--reps",
            type=int,
            default=3,
            help="Repetitions per case (default 3: these cases flaked in-app).",
        )
        parser.add_argument(
            "--case", action="append", default=[], help="Run only this case id (repeatable)."
        )
        parser.add_argument(
            "--model", default="", help="The client's model (default: settings CLAUDE_MODEL)."
        )
        parser.add_argument(
            "--cost-cap-usd",
            type=float,
            default=0.0,
            help="Hard stop for the run, from catalog prices.",
        )
        parser.add_argument(
            "--ken-approved",
            action="store_true",
            help="Required for a billed run: the maintainer has said go for it.",
        )
        parser.add_argument(
            "--json", dest="json_path", default="", help="Write one JSON line per repetition here."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        cases = driver.load_cases()
        if options["case"]:
            wanted = set(options["case"])
            unknown = wanted - {c.id for c in cases}
            if unknown:
                raise CommandError(f"unknown case id(s): {sorted(unknown)}")
            cases = [c for c in cases if c.id in wanted]
        reps = max(int(options["reps"]), 1)
        model = options["model"] or self._default_model()
        catalog = driver.client_catalog()
        plan = driver.estimate(cases, reps=reps, model=model, catalog=catalog)
        self._print_plan(plan, cases)
        if options["dry_run"]:
            return
        if not options["ken_approved"]:
            raise CommandError(
                "A billed run needs --ken-approved (the maintainer's go). Use --dry-run to see the plan."
            )
        cap = float(options["cost_cap_usd"] or 0)
        if cap <= 0:
            raise CommandError("A billed run needs --cost-cap-usd.")
        if plan["usd_total"] is not None and plan["usd_total"] > cap:
            raise CommandError(
                f"The estimate (${plan['usd_total']:.2f}) is above --cost-cap-usd (${cap:.2f}); raise the cap or cut --reps."
            )
        self._run(
            cases, reps=reps, model=model, catalog=catalog, cap=cap, json_path=options["json_path"]
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_model() -> str:
        from django.conf import settings  # noqa: PLC0415

        return str(settings.SEARCH_ENGINE.get("CLAUDE_MODEL") or "claude-sonnet-5-5")

    def _print_plan(self, plan: dict[str, Any], cases: list[Any]) -> None:
        w = self.stdout.write
        w("=== agent_eval_mcp_injection plan (no model call yet) ===")
        w(
            f"client model: {plan['model']}; {plan['cases']} cases x {plan['reps']} reps = {plan['runs']} runs"
        )
        w(f"tool declarations sent every turn: ~{plan['declaration_tokens']} tokens")
        usd = plan["usd_total"]
        w(
            "estimated cost: "
            + (
                "no catalog price for this model"
                if usd is None
                else f"~${usd:.2f} (${plan['usd_per_run']:.3f} per run)"
            )
        )
        w(f"assumptions: {plan['assumptions']}")
        for c in cases:
            w(f"  {c.id}  [{c.place}, {c.kind}]")

    def _run(
        self,
        cases: list[Any],
        *,
        reps: int,
        model: str,
        catalog: list[Any],
        cap: float,
        json_path: str,
    ) -> None:
        from origin.search_engine.agent.evals.group import case_runner  # noqa: PLC0415

        info = case_runner.resolve_sequence("v2")
        refs = driver.seed_all(info)
        writes = driver.write_tool_names()
        client = self.client_factory()
        spent = {"usd": 0.0, "last_in": 0, "last_out": 0}

        def on_usage(total_in: int, total_out: int) -> bool:
            # `run_case` reports the running totals of ONE run; add the delta since the last call.
            spent["usd"] += (
                group_usage.estimate_cost_usd(
                    model,
                    input_tokens=max(total_in - spent["last_in"], 0),
                    output_tokens=max(total_out - spent["last_out"], 0),
                )
                or 0.0
            )
            spent["last_in"], spent["last_out"] = total_in, total_out
            return spent["usd"] < cap

        out: Path | None = Path(json_path) if json_path else None
        rows: list[dict[str, Any]] = []
        for case in cases:
            for rep in range(1, reps + 1):
                spent["last_in"] = spent["last_out"] = 0
                run = driver.run_case(
                    case,
                    refs=refs,
                    info=info,
                    client=client,
                    model=model,
                    catalog=catalog,
                    writes=writes,
                    on_usage=on_usage,
                )
                checks = driver.check_run(case, run)
                ok = driver.passed(run, checks)
                rows.append(
                    {
                        "case_id": case.id,
                        "rep": rep,
                        "place": case.place,
                        "kind": case.kind,
                        "passed": ok,
                        "stop": run.stop,
                        "answer": run.answer,
                        "tool_calls": run.tool_calls,
                        "proposed_writes": run.proposed_writes,
                        "checks": checks,
                        "input_tokens": run.input_tokens,
                        "output_tokens": run.output_tokens,
                        "error": run.error,
                    }
                )
                self.stdout.write(
                    f"{'PASS' if ok else 'FAIL'}  {case.id} rep {rep}  stop={run.stop}  ~${spent['usd']:.2f} so far"
                )
                if run.stop == "cost_cap":
                    self.stdout.write("cost cap reached: stopping.")
                    return self._finish(rows, out, spent["usd"])
        self._finish(rows, out, spent["usd"])

    def _finish(self, rows: list[dict[str, Any]], out: Path | None, usd: float) -> None:
        if out is not None:
            out.write_text(
                "\n".join(json.dumps(r, default=str) for r in rows) + "\n", encoding="utf-8"
            )
            self.stdout.write(f"wrote {len(rows)} repetitions to {out}")
        passed = sum(1 for r in rows if r["passed"])
        self.stdout.write(
            f"{passed}/{len(rows)} repetitions passed the deterministic checks; ~${usd:.2f} spent (estimate)."
        )
        self.stdout.write("Next: Claude judges the JSONL with group/injection_rubric.md.")


def _anthropic_client() -> Any:
    from origin.search_engine.llm.claude_client import _get_client  # noqa: PLC0415

    return _get_client()

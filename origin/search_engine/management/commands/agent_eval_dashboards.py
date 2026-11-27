"""`python manage.py agent_eval_dashboards`: the dashboard behaviour cases (MIP-2378).

    # what would it cost? NO model call, no database, no credentials
    python manage.py agent_eval_dashboards --dry-run

    # the billed run: needs the maintainer's go (--ken-approved) and a hard cap; never CI
    python manage.py agent_eval_dashboards --reps 3 --cost-cap-usd 3 --ken-approved --json run.jsonl

Two suites (`--suite`, default both):

  mcp    `group/mcp/t2361-mcp-dashboards.yaml`, driver `group/mcp_dashboards.py`: a Claude model as the
         connected client, the real MCP catalog and handlers.
  skill  `group/skills/t2409-dashboard-page.yaml`, driver `group/skill_dashboard_eval.py`: the scheduled
         skill run of the in-app agent, on the model the schedule uses.

Needs the v1 eval fixture (`agent_eval_setup`); this command never builds it, because building it reindexes
with billed embeddings. Every case runs in a transaction that is rolled back. The JSONL keeps every tool call
and answer: Claude judges it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine.agent.evals.group import (
    mcp_dashboards,
    mcp_injection,
    skill_dashboard_eval,
)
from origin.search_engine.agent.evals.group import usage as group_usage

SUITES = ("mcp", "skill")

#: Assumptions of the skill suite's estimate (the in-app agent re-sends its prompt and tool list on every
#: call); they are printed with the number.
SKILL_CALLS = 5
SKILL_INPUT_TOKENS_PER_CALL = 40_000
SKILL_OUTPUT_TOKENS_PER_CALL = 800


class Command(BaseCommand):
    help = "Dashboard behaviour cases: the MCP surface and the dashboard-page skill. Billed."

    #: injectable for tests: `() -> anthropic-shaped client`
    client_factory = staticmethod(lambda: _anthropic_client())

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan and the cost estimate; call nothing.",
        )
        parser.add_argument("--suite", choices=("all", *SUITES), default="all")
        parser.add_argument("--reps", type=int, default=3, help="Repetitions per case (default 3).")
        parser.add_argument(
            "--case", action="append", default=[], help="Run only this case id (repeatable)."
        )
        parser.add_argument(
            "--model", default="", help="The MCP client's model (default: settings CLAUDE_MODEL)."
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
        suites = SUITES if options["suite"] == "all" else (options["suite"],)
        mcp_cases = mcp_dashboards.load_cases() if "mcp" in suites else []
        skill_cases = skill_dashboard_eval.load_cases() if "skill" in suites else []
        if options["case"]:
            wanted = set(options["case"])
            unknown = wanted - {c.id for c in [*mcp_cases, *skill_cases]}
            if unknown:
                raise CommandError(f"unknown case id(s): {sorted(unknown)}")
            mcp_cases = [c for c in mcp_cases if c.id in wanted]
            skill_cases = [c for c in skill_cases if c.id in wanted]
        reps = max(int(options["reps"]), 1)
        model = options["model"] or self._default_model()
        catalog = mcp_injection.client_catalog() if mcp_cases else []
        total = self._print_plan(mcp_cases, skill_cases, reps=reps, model=model, catalog=catalog)
        if options["dry_run"]:
            return
        if not options["ken_approved"]:
            raise CommandError(
                "A billed run needs --ken-approved (the maintainer's go). Use --dry-run to see the plan."
            )
        cap = float(options["cost_cap_usd"] or 0)
        if cap <= 0:
            raise CommandError("A billed run needs --cost-cap-usd.")
        if total is None:
            raise CommandError(
                "A model has no catalog price: the cost cap could not be enforced. Use a priced model."
            )
        if total > cap:
            raise CommandError(
                f"The estimate (${total:.2f}) is above --cost-cap-usd (${cap:.2f}); raise the cap or cut --reps."
            )
        self._run(
            mcp_cases,
            skill_cases,
            reps=reps,
            model=model,
            catalog=catalog,
            cap=cap,
            json_path=options["json_path"],
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_model() -> str:
        from django.conf import settings  # noqa: PLC0415

        return str(settings.SEARCH_ENGINE.get("CLAUDE_MODEL") or "claude-sonnet-5-5")

    def _print_plan(
        self,
        mcp_cases: list[Any],
        skill_cases: list[Any],
        *,
        reps: int,
        model: str,
        catalog: list[Any],
    ) -> float | None:
        """Print the plan; return the estimated total, None when a model has no price."""
        w = self.stdout.write
        w("=== agent_eval_dashboards plan (no model call yet) ===")
        parts: list[float | None] = []
        if mcp_cases:
            plan = mcp_injection.estimate(mcp_cases, reps=reps, model=model, catalog=catalog)
            parts.append(plan["usd_total"])
            w(
                f"mcp suite: client model {model}; {plan['cases']} cases x {reps} reps = "
                f"{plan['runs']} runs; tool declarations ~{plan['declaration_tokens']} tokens a turn; "
                f"estimate {_money(plan['usd_total'])}"
            )
            w(f"  assumptions: {plan['assumptions']}")
            for case in mcp_cases:
                w(f"  {case.id}  [{case.plant}]")
        if skill_cases:
            chosen = skill_dashboard_eval.run_settings()
            if chosen is None:
                raise CommandError("The catalog has no model for the dashboard schedule's effort.")
            per_run = group_usage.estimate_cost_usd(
                chosen.model,
                input_tokens=SKILL_CALLS * SKILL_INPUT_TOKENS_PER_CALL,
                output_tokens=SKILL_CALLS * SKILL_OUTPUT_TOKENS_PER_CALL,
            )
            runs = len(skill_cases) * reps
            subtotal = None if per_run is None else per_run * runs
            parts.append(subtotal)
            w(
                f"skill suite: model {chosen.model} (effort {chosen.effort}); {len(skill_cases)} cases "
                f"x {reps} reps = {runs} runs; estimate {_money(subtotal)}"
            )
            w(
                f"  assumptions: {SKILL_CALLS} model calls a run, {SKILL_INPUT_TOKENS_PER_CALL} input "
                f"and {SKILL_OUTPUT_TOKENS_PER_CALL} output tokens a call, no prompt caching"
            )
            for case in skill_cases:
                w(f"  {case.id}  [skill {case.skill}]")
        total = None if any(p is None for p in parts) else sum(p for p in parts if p is not None)
        w(f"estimated cost in total: {_money(total)}")
        return total

    def _run(
        self,
        mcp_cases: list[Any],
        skill_cases: list[Any],
        *,
        reps: int,
        model: str,
        catalog: list[Any],
        cap: float,
        json_path: str,
    ) -> None:
        from origin.search_engine.agent.evals.group import harness  # noqa: PLC0415

        info = fixture_info()
        # The MCP client is not one of the app's adapters, so its cost is counted here; the guard sees
        # every other priced call of the process.
        self._client_usd = 0.0
        guard = harness.SessionSpend(cap)
        rows: list[dict[str, Any]] = []
        try:
            with group_usage.listen(guard.on_record), harness.guarded_llm(guard):
                if mcp_cases:
                    self._run_mcp(rows, mcp_cases, reps, info, model, catalog, guard, cap)
                if skill_cases:
                    guard.cap_usd = max(cap - self._client_usd, 0.0)  # what is left
                    self._run_skill(rows, skill_cases, reps, info, guard)
        except harness.CostCapReached:
            self.stdout.write("cost cap reached: stopping.")
        finally:
            out = Path(json_path) if json_path else None
            self._finish(rows, out, self._client_usd + guard.spent)

    def _run_mcp(
        self,
        rows: list[dict[str, Any]],
        cases: list[Any],
        reps: int,
        info: dict[str, Any],
        model: str,
        catalog: list[Any],
        guard: Any,
        cap: float,
    ) -> None:
        from origin.search_engine.agent.evals.group import harness  # noqa: PLC0415

        writes = mcp_injection.write_tool_names()
        client = self.client_factory()

        def usage_meter() -> Callable[[int, int], bool]:
            seen = [0, 0]  # `run_case` reports the running totals of ONE run

            def on_usage(total_in: int, total_out: int) -> bool:
                self._client_usd += (
                    group_usage.estimate_cost_usd(
                        model,
                        input_tokens=max(total_in - seen[0], 0),
                        output_tokens=max(total_out - seen[1], 0),
                    )
                    or 0.0
                )
                seen[:] = total_in, total_out
                return self._client_usd + guard.spent < cap

            return on_usage

        for case in cases:
            for rep in range(1, reps + 1):
                run = mcp_dashboards.run_case(
                    case,
                    info=info,
                    client=client,
                    model=model,
                    catalog=catalog,
                    writes=writes,
                    on_usage=usage_meter(),
                )
                checks = mcp_dashboards.check_run(case, run, writes=writes)
                ok = mcp_dashboards.passed(run, checks)
                self._row(rows, "mcp", case, rep, ok, checks, asdict(run))
                if run.stop == "cost_cap":
                    raise harness.CostCapReached("mcp client")

    def _run_skill(
        self,
        rows: list[dict[str, Any]],
        cases: list[Any],
        reps: int,
        info: dict[str, Any],
        guard: Any,
    ) -> None:
        from origin.search_engine.agent.evals.group import harness  # noqa: PLC0415

        for case in cases:
            for rep in range(1, reps + 1):
                run = skill_dashboard_eval.run_case(case, info=info)
                # The agent loop turns a refused call into a failed run: read the guard, not the run.
                if guard.tripped:
                    raise harness.CostCapReached(guard.tripped)
                checks = skill_dashboard_eval.check_run(case, run)
                ok = skill_dashboard_eval.passed(run, checks)
                self._row(rows, "skill", case, rep, ok, checks, asdict(run))

    def _row(
        self,
        rows: list[dict[str, Any]],
        suite: str,
        case: Any,
        rep: int,
        ok: bool,
        checks: list[dict[str, Any]],
        run: dict[str, Any],
    ) -> None:
        rows.append({"suite": suite, "rep": rep, "passed": ok, "checks": checks, **run})
        failed = ", ".join(c["name"] for c in checks if not c["ok"])
        self.stdout.write(
            f"{'PASS' if ok else 'FAIL'}  {case.id} rep {rep}" + (f"  ({failed})" if failed else "")
        )

    def _finish(self, rows: list[dict[str, Any]], out: Path | None, usd: float) -> None:
        if out is not None and rows:
            out.write_text(
                "\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
            self.stdout.write(f"wrote {len(rows)} repetitions to {out}")
        passed = sum(1 for r in rows if r["passed"])
        self.stdout.write(
            f"{passed}/{len(rows)} repetitions passed the deterministic checks; ~${usd:.2f} spent (estimate)."
        )
        self.stdout.write("Next: Claude judges the JSONL.")


def fixture_info() -> dict[str, str]:
    """The v1 eval fixture's team and viewer. Never builds it (that reindexes with billed embeddings)."""
    from origin.models.common.team_models import TeamMaster  # noqa: PLC0415
    from origin.search_engine.agent.evals.fixture import FIXTURE_USER_ID  # noqa: PLC0415

    team = TeamMaster.objects.filter(owner_id=FIXTURE_USER_ID, is_demo=True).first()
    if team is None:
        raise CommandError(
            "The v1 eval fixture is not seeded: run `python manage.py agent_eval_setup`."
        )
    return {"team_id": str(team.team_id), "user_id": str(FIXTURE_USER_ID)}


def _money(usd: float | None) -> str:
    return "no catalog price" if usd is None else f"~${usd:.2f}"


def _anthropic_client() -> Any:
    from origin.search_engine.llm.claude_client import _get_client  # noqa: PLC0415

    return _get_client()

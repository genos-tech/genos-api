"""`python manage.py ai_credit_benchmark` — measured cost, per scenario.

V2 §6.4: at ~zero customers, a week of production traffic proves
nothing, so the credit numbers are validated against a REPRESENTATIVE
benchmark instead — the existing behavior suite (single-turn, tool-
using, and multi-turn cases), driven across a provider × effort matrix,
with per-case cost read back from the spend ledger.

    ai_credit_benchmark --cases simple_fact_lookup,multi_tool_plan
    ai_credit_benchmark --providers gemini --efforts low,medium
    ai_credit_benchmark --write-baseline          # pin today's tokens
    ai_credit_benchmark --check-baseline          # exit 1 on regression

⚠️ THIS SPENDS REAL MONEY — every cell is a live agent run against the
configured providers. It is a deliberate operator action (the same
posture as the Phase 0 live-provider proof), not a cron and not CI. It
requires AI_COST_METER=true, because the ledger is where the numbers
come from; without it every cell would read ¥0 and the output would be
confident nonsense.

WHAT IT REPORTS

  * per (provider, effort): tokens / yen / credits per case, and
    nearest-rank P50/75/90/95/max across the cells;
  * each plan's 10/40/100/200 allowance expressed in "requests like
    these" — the first measured answer to whether ¥15/credit and the
    V2 scale survive contact with reality;
  * failures are kept and labelled, not averaged in: a failed cell's
    cost is real (we absorb it) but it is not a "typical request".

THE REGRESSION ALARM (V2 §6.4's "the fixed benchmark does not become
materially more expensive after a deployment without an alert")

  * `--write-baseline` stores per-case TOKEN totals (medium effort,
    default provider) in evals/cost_baseline.json.
  * `--check-baseline` re-runs those cases and exits non-zero when
    total tokens exceed the baseline by more than
    AI_BENCHMARK_REGRESSION_PCT (default 25%).
  * TOKENS, deliberately, not yen: yen moves when the rate card or FX
    pin moves, and a price change reading as an engineering regression
    is the exact confusion this alarm exists to prevent. The rate card
    at baseline time is recorded and printed for context only.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from origin.search_engine.agent.evals.runner import BEHAVIOR_CASES_PATH, run_behavior_case
from origin.search_engine.llm.choice import LlmChoice, reset_llm_choice, set_llm_choice

BASELINE_PATH = Path(BEHAVIOR_CASES_PATH).parent / "cost_baseline.json"

# The default matrix subset: one representative case per V2 §6.4
# request class, kept small because every cell is real spend. Operators
# widen it with --cases.
_DEFAULT_CASE_LIMIT = 3


def _percentiles(values: list[int]) -> str:
    if not values:
        return "(none)"
    ordered = sorted(values)

    def rank(p: int) -> int:
        return ordered[max(0, min(len(ordered) - 1, round(p / 100 * len(ordered)) - 1))]

    return (
        f"p50={rank(50):,}  p75={rank(75):,}  p90={rank(90):,}  "
        f"p95={rank(95):,}  max={ordered[-1]:,}"
    )


class Command(BaseCommand):
    help = "Run the cost benchmark matrix over the behavior suite (SPENDS REAL MONEY)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cases",
            default=None,
            help=(
                "Comma-separated behavior case ids to run. Default: the first "
                f"{_DEFAULT_CASE_LIMIT} cases of the suite (keep the matrix small — "
                "every cell is a live run)."
            ),
        )
        parser.add_argument(
            "--providers",
            default=None,
            help="Comma-separated providers (default: every provider in the catalog).",
        )
        parser.add_argument(
            "--efforts",
            default="low,medium,high",
            help="Comma-separated efforts (default low,medium,high).",
        )
        parser.add_argument(
            "--write-baseline",
            action="store_true",
            help="Store per-case token totals as the regression baseline.",
        )
        parser.add_argument(
            "--check-baseline",
            action="store_true",
            help=(
                "Compare against the stored baseline; exit 1 when total tokens "
                "regress past AI_BENCHMARK_REGRESSION_PCT (default 25%%)."
            ),
        )

    # ------------------------------------------------------------------ #

    def handle(self, *args, **options):
        if not settings.SEARCH_ENGINE.get("AI_COST_METER"):
            raise CommandError(
                "AI_COST_METER is off — the benchmark reads its numbers from the "
                "spend ledger, and with the meter off every cell would report ¥0. "
                "Run with AI_COST_METER=true."
            )

        cases = self._load_cases(options.get("cases"))

        if options.get("write_baseline") or options.get("check_baseline"):
            self._baseline(cases, write=bool(options.get("write_baseline")))
            return

        providers = (
            [p.strip() for p in options["providers"].split(",") if p.strip()]
            if options.get("providers")
            else settings.LLM_CATALOG.provider_order()
        )
        efforts = [e.strip() for e in options["efforts"].split(",") if e.strip()]

        self.stdout.write(
            f"=== credit benchmark — {len(cases)} case(s) × "
            f"{len(providers)} provider(s) × {len(efforts)} effort(s) ===\n"
            f"⚠ every cell is a live provider run."
        )

        policy = settings.CREDIT_POLICY
        all_credits: list[int] = []
        for provider in providers:
            for effort in efforts:
                cells = self._run_cells(cases, provider=provider, effort=effort)
                ok = [c for c in cells if not c["failed"]]
                failed = len(cells) - len(ok)
                tokens = [c["total_tokens"] for c in ok]
                credits_list = [c["credits_milli"] for c in ok]
                all_credits.extend(credits_list)
                self.stdout.write(f"\n-- {provider} / {effort} --")

                if not ok:
                    # An ENTIRELY dead cell. Say so instead of printing a
                    # table of zeros: a rung that cannot serve a single
                    # request is a capability failure to investigate, not
                    # a measurement, and rendering it as 0.00cr invites
                    # exactly the wrong reading.
                    reasons = sorted({c["reason"] for c in cells if c["reason"]})
                    self.stdout.write(
                        self.style.ERROR(
                            f"  NO USABLE RUNS — all {len(cells)} case(s) failed "
                            f"({', '.join(reasons) or 'unknown'}). This rung is "
                            f"excluded from every figure below; it is a capability "
                            f"problem, not a cheap provider."
                        )
                    )
                    continue

                self.stdout.write(f"  tokens/case   {_percentiles(tokens)}")
                self.stdout.write(
                    "  credits/case  "
                    + (
                        "  ".join(
                            f"{k}={v / 1000:,.2f}cr"
                            for k, v in zip(
                                ("p50", "mean", "max"),
                                (
                                    sorted(credits_list)[len(credits_list) // 2],
                                    statistics.mean(credits_list),
                                    max(credits_list),
                                ),
                            )
                        )
                        if credits_list
                        else "(none)"
                    )
                )
                if failed:
                    reasons = sorted({c["reason"] for c in cells if c["reason"]})
                    self.stdout.write(
                        self.style.WARNING(
                            f"  {failed} failed cell(s) ({', '.join(reasons)}) — "
                            f"excluded from the percentiles and from the mean. "
                            f"A request the provider never really ran is an absent "
                            f"request, not a cheap one."
                        )
                    )

        if all_credits:
            mean_milli = statistics.mean(all_credits)
            self.stdout.write("\n-- Allowances, in requests like these --")
            for plan in ("free", "core", "pro", "max"):
                entitlement = policy.entitlements_milli.get(plan)
                if entitlement and mean_milli:
                    self.stdout.write(
                        f"  {plan:<6} {entitlement / 1000:>6.0f}cr ≈ "
                        f"{entitlement / mean_milli:,.0f} request(s)/month"
                    )
            self.stdout.write(
                f"  (mean {mean_milli / 1000:,.2f}cr per successful benchmark request; "
                f"policy {policy.version})"
            )

    # ------------------------------------------------------------------ #

    def _load_cases(self, spec: str | None) -> list[dict]:
        with open(BEHAVIOR_CASES_PATH, encoding="utf-8") as f:
            all_cases = yaml.safe_load(f) or []
        if spec:
            wanted = [c.strip() for c in spec.split(",") if c.strip()]
            by_id = {c.get("id"): c for c in all_cases}
            missing = [w for w in wanted if w not in by_id]
            if missing:
                raise CommandError(f"unknown case id(s): {', '.join(missing)}")
            return [by_id[w] for w in wanted]
        return all_cases[:_DEFAULT_CASE_LIMIT]

    def _run_cells(self, cases: list[dict], *, provider: str, effort: str) -> list[dict]:
        """One (provider, effort) row: run each case under that choice.

        The choice is bound exactly the way the ask view binds a user's
        preference, so the benchmark measures the same code path a
        customer pays for. Cost comes back from the ledger via the
        runner's own `_case_cost` (surface="eval").
        """
        model = settings.LLM_CATALOG.model_for_effort(provider, effort)
        cells = []
        for case in cases:
            token = set_llm_choice(LlmChoice(provider=provider, model=model, effort=effort))
            try:
                result = run_behavior_case(case)
            except Exception as e:  # noqa: BLE001 — one dead cell must not kill the matrix
                cells.append(
                    {
                        "case": case.get("id"),
                        "failed": True,
                        "reason": f"raised: {type(e).__name__}",
                        "total_tokens": 0,
                        "cost_jpy_milli": 0,
                        "credits_milli": 0,
                    }
                )
                continue
            finally:
                reset_llm_choice(token)
            jpy = int(getattr(result, "cost_jpy_milli", 0) or 0)
            tokens = int(getattr(result, "total_tokens", 0) or 0)
            # A cell counts as FAILED when the provider never really ran
            # it. Two signals, and the second is the one that matters:
            #
            #   * `infra_error` — the runner's own "provider 429/5xx".
            #   * ZERO TOKENS — the run completed but no provider call
            #     produced usage. That is not a cheap request, it is an
            #     absent one, and averaging it in silently DEFLATES the
            #     credit-per-request figure this command exists to
            #     produce. It is how a provider that answers nothing
            #     reads as the most efficient provider we have.
            #
            # Deliberately NOT `passed`: that is the eval ASSERTION, and
            # a case can answer badly while costing exactly what a real
            # request costs. Quality is not this command's question.
            failed = bool(getattr(result, "infra_error", False)) or tokens == 0
            cells.append(
                {
                    "case": case.get("id"),
                    "failed": failed,
                    "reason": (
                        "infra_error"
                        if getattr(result, "infra_error", False)
                        else ("no provider usage" if tokens == 0 else "")
                    ),
                    "total_tokens": tokens,
                    "cost_jpy_milli": jpy,
                    "credits_milli": int(
                        round(jpy / settings.CREDIT_POLICY.credit_jpy)
                    ),
                }
            )
        return cells

    # ------------------------------------------------------------------ #

    def _baseline(self, cases: list[dict], *, write: bool) -> None:
        """The fixed-suite regression alarm, in TOKENS.

        Baseline runs use the server-default provider at medium effort —
        the configuration customers actually get — so the number moves
        only when the AGENT'S BEHAVIOUR moves.
        """
        provider = (settings.SEARCH_ENGINE.get("LLM_PROVIDER") or "gemini").lower()
        cells = self._run_cells(cases, provider=provider, effort="medium")
        ok = [c for c in cells if not c["failed"]]
        totals = {c["case"]: c["total_tokens"] for c in ok}
        grand = sum(totals.values())

        if write:
            card = settings.LLM_CATALOG.rate_card
            BASELINE_PATH.write_text(
                json.dumps(
                    {
                        "provider": provider,
                        "effort": "medium",
                        "per_case_tokens": totals,
                        "total_tokens": grand,
                        # Context only — the comparison NEVER uses yen.
                        "rate_card_at_baseline": card.version if card else "",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Baseline written: {len(totals)} case(s), {grand:,} tokens "
                    f"({BASELINE_PATH})"
                )
            )
            return

        if not BASELINE_PATH.exists():
            raise CommandError(
                f"No baseline at {BASELINE_PATH} — run --write-baseline first."
            )
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        base_total = int(baseline.get("total_tokens") or 0)
        threshold_pct = float(
            settings.SEARCH_ENGINE.get("AI_BENCHMARK_REGRESSION_PCT", 25) or 25
        )
        delta_pct = 100 * (grand - base_total) / base_total if base_total else 0.0
        self.stdout.write(
            f"tokens: baseline {base_total:,} → now {grand:,} ({delta_pct:+.0f}%), "
            f"threshold +{threshold_pct:.0f}%"
        )
        for case_id, base_tokens in sorted(baseline.get("per_case_tokens", {}).items()):
            now = totals.get(case_id)
            if now is None:
                self.stdout.write(self.style.WARNING(f"  {case_id}: missing from this run"))
                continue
            self.stdout.write(f"  {case_id}: {base_tokens:,} → {now:,}")
        if base_total and delta_pct > threshold_pct:
            self.stderr.write(
                self.style.ERROR(
                    f"COST REGRESSION: the fixed suite got {delta_pct:.0f}% more "
                    f"expensive in tokens. A price change cannot cause this — "
                    f"the agent's behaviour moved."
                )
            )
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS("within the regression threshold."))

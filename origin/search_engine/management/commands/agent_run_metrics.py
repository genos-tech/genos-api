"""`python manage.py agent_run_metrics` — offline agent performance/cost report.

Rolls up the per-call telemetry captured during agent runs
(`AgentLlmCall`) plus tool timings (`AgentStep.latency_ms`) and
end-to-end run elapsed (`AgentRun.started_at/finished_at`) into a
readable performance + cost breakdown.

Runs entirely OFF the user request path — the capture during a run is
cheap (numbers the provider already returns + a `monotonic` delta), and
ALL the heavy aggregation lives here, so it can be scheduled or run
ad hoc without touching latency. There is no async/Celery infra in this
project, so like `agent_judge_sample` it's a plain management command:

    python manage.py agent_run_metrics                    # last 7 days
    python manage.py agent_run_metrics --days 30 --team <id>
    python manage.py agent_run_metrics --top-tools 25

What it answers:
  * Which LLM call is the latency/cost bottleneck — per model AND per
    purpose (loop / planning / synthesis), so a B3 planning split is
    visible as its own line.
  * Which TOOLS are slow — per tool_name latency, from AgentStep.
  * How many tokens (and roughly how many dollars) a window burned.

Cost is a DERIVED ESTIMATE priced from the catalog rate card
(`apis/llm_models.yaml`) — LLM-API spend only, excluding the fixed
infra floor that actually dominates the bill (see LLM_SPEND_ANATOMY).
Tokens and latency are ground truth; treat the dollar figure as an
order-of-magnitude guide.

This command reports LATENCY. It is not the accounting ledger, and it
sees only the agent loop — the rewriter, reranker, summaries, evals,
embeddings and paid tools never reach `AgentLlmCall`. Never add its
totals to the cost ledger's: they describe overlapping calls from two
different tables.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from origin.search_engine.models import AgentLlmCall, AgentRun, AgentStep


def _cost_usd(model: str, prompt: int, cached: int, cache_write: int, output: int) -> float | None:
    """Derived per-call cost estimate, or None for an unpriced model.

    `thought`/`tool_prompt` tokens fold into `output`/`prompt` at the
    caller — this takes the four billable buckets directly.

    Priced from the catalog by EXACT model id. This used to be a
    hand-maintained sheet in this file matched by longest name prefix,
    and it had rotted badly enough to be useless: `"gemini-3.6-flash"`
    does not start with `"gemini-3-flash"` (dot vs hyphen), so in
    production — where Gemini is the default provider — every Gemini
    and every GPT call priced as unpriced and the report simply left
    them out of the total. Of the models that did still match, Opus was
    listed at 3x its real rate. One price source, exact ids, and a test
    that walks the whole catalog is the fix for all of that.
    """
    catalog = getattr(settings, "LLM_CATALOG", None)
    price = catalog.price_for(model) if catalog is not None else None
    if price is None:
        return None
    return (
        prompt * price.input
        + cached * price.cached_input
        + cache_write * price.cache_write
        + output * price.output
    ) / 1_000_000.0


def _pct(values: list[int], pct: float) -> int:
    """Nearest-rank percentile over a list of ints (0 when empty).

    Computed in Python rather than SQL so the report is portable across
    DB backends and needs no Postgres-only aggregate. The input is
    bounded by the reporting window, and this is an offline command, so
    pulling the raw values is fine.
    """
    if not values:
        return 0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


class Command(BaseCommand):
    help = "Offline performance + cost report over captured agent-run telemetry."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Lookback window in days (default 7).",
        )
        parser.add_argument(
            "--team",
            dest="team_id",
            default=None,
            help="Restrict to a single team_id.",
        )
        parser.add_argument(
            "--top-tools",
            type=int,
            default=15,
            help="How many tools to show in the tool-latency table (default 15).",
        )

    def handle(self, *args, **options):
        days: int = options["days"]
        team_id: str | None = options.get("team_id")
        top_tools: int = options["top_tools"]
        cutoff = timezone.now() - timedelta(days=days)

        calls = AgentLlmCall.objects.filter(created_at__gte=cutoff)
        runs = AgentRun.objects.filter(started_at__gte=cutoff, finished_at__isnull=False)
        tool_steps = AgentStep.objects.filter(run__started_at__gte=cutoff).exclude(tool_name="")
        if team_id:
            calls = calls.filter(team_id=team_id)
            runs = runs.filter(team_id=team_id)
            tool_steps = tool_steps.filter(run__team_id=team_id)

        header = f"=== Agent run metrics — last {days} day(s)" + (
            f", team {team_id}" if team_id else ""
        )
        self.stdout.write(header + " ===")

        if not calls.exists() and not runs.exists():
            self.stdout.write(
                self.style.WARNING(
                    "No telemetry in this window. Runs capture it automatically "
                    "(AGENT_COLLECT_METRICS on by default); widen --days or check "
                    "that recent runs exist."
                )
            )
            return

        self._run_elapsed_section(runs)
        self._per_model_section(calls)
        self._per_purpose_section(calls)
        self._tool_section(tool_steps, top_tools)
        self._totals_section(calls)

    # ---------------------------------------------------------------- #
    # Sections                                                         #
    # ---------------------------------------------------------------- #

    def _run_elapsed_section(self, runs) -> None:
        pairs = list(runs.values_list("started_at", "finished_at", "status"))
        durations = [
            int((fin - start).total_seconds() * 1000)
            for start, fin, _status in pairs
            if start and fin and fin >= start
        ]
        by_status: dict[str, int] = {}
        for _s, _f, status in pairs:
            by_status[status] = by_status.get(status, 0) + 1
        self.stdout.write(f"\n-- Runs ({len(pairs)} completed) --")
        if durations:
            self.stdout.write(
                f"  end-to-end elapsed ms:  avg={sum(durations) // len(durations)}  "
                f"p50={_pct(durations, 50)}  p95={_pct(durations, 95)}  max={max(durations)}"
            )
        if by_status:
            status_line = "  ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
            self.stdout.write(f"  by status:  {status_line}")

    def _per_model_section(self, calls) -> None:
        self.stdout.write("\n-- LLM calls by model --")
        rows = (
            calls.values("provider", "model")
            .annotate(
                n=Count("id"),
                prompt=Sum("prompt_tokens"),
                cached=Sum("cached_tokens"),
                cache_write=Sum("cache_write_tokens"),
                output=Sum("output_tokens"),
                thought=Sum("thought_tokens"),
                tool_prompt=Sum("tool_prompt_tokens"),
            )
            .order_by("-n")
        )
        if not rows:
            self.stdout.write("  (none)")
            return
        self.stdout.write(
            f"  {'model':<28} {'n':>5} {'lat p50/p95':>13} "
            f"{'in/cache/out ktok':>20} {'~$':>8}"
        )
        for r in rows:
            model = r["model"] or "(unknown)"
            lat = list(
                calls.filter(provider=r["provider"], model=r["model"]).values_list(
                    "latency_ms", flat=True
                )
            )
            prompt = r["prompt"] or 0
            cached = r["cached"] or 0
            cache_write = r["cache_write"] or 0
            output = (r["output"] or 0) + (r["thought"] or 0)
            billable_in = prompt + (r["tool_prompt"] or 0)
            cost = _cost_usd(model, billable_in, cached, cache_write, output)
            cost_cell = f"{cost:>8.2f}" if cost is not None else f"{'n/a':>8}"
            ktok = f"{billable_in / 1000:.0f}/{cached / 1000:.0f}/{output / 1000:.0f}"
            self.stdout.write(
                f"  {model[:28]:<28} {r['n']:>5} "
                f"{_pct(lat, 50):>5}/{_pct(lat, 95):<7} {ktok:>20} {cost_cell}"
            )

    def _per_purpose_section(self, calls) -> None:
        self.stdout.write("\n-- LLM calls by purpose --")
        rows = calls.values("purpose").annotate(n=Count("id")).order_by("-n")
        if not rows:
            self.stdout.write("  (none)")
            return
        for r in rows:
            purpose = r["purpose"] or "(unset)"
            lat = list(calls.filter(purpose=r["purpose"]).values_list("latency_ms", flat=True))
            self.stdout.write(
                f"  {purpose:<12} n={r['n']:<6} "
                f"lat p50={_pct(lat, 50)}ms p95={_pct(lat, 95)}ms"
            )

    def _tool_section(self, tool_steps, top: int) -> None:
        self.stdout.write("\n-- Tool execution latency (top by count) --")
        rows = tool_steps.values("tool_name").annotate(n=Count("step_id")).order_by("-n")[:top]
        if not rows:
            self.stdout.write("  (no tool calls)")
            return
        self.stdout.write(f"  {'tool':<32} {'n':>5} {'lat p50/p95/max ms':>22}")
        for r in rows:
            name = r["tool_name"]
            lat = list(tool_steps.filter(tool_name=name).values_list("latency_ms", flat=True))
            self.stdout.write(
                f"  {name[:32]:<32} {r['n']:>5} "
                f"{_pct(lat, 50):>6}/{_pct(lat, 95)}/{max(lat) if lat else 0}"
            )

    def _totals_section(self, calls) -> None:
        agg = calls.aggregate(
            n=Count("id"),
            prompt=Sum("prompt_tokens"),
            cached=Sum("cached_tokens"),
            cache_write=Sum("cache_write_tokens"),
            output=Sum("output_tokens"),
            thought=Sum("thought_tokens"),
            tool_prompt=Sum("tool_prompt_tokens"),
        )
        total_tokens = sum(
            (agg[k] or 0)
            for k in ("prompt", "cached", "cache_write", "output", "thought", "tool_prompt")
        )
        # Sum per-model derived cost so unpriced models simply don't
        # contribute (rather than poisoning the total with a 0). But
        # NAME them below — a silently-dropped model is how a whole
        # provider goes missing from a total that still looks about
        # right, which is exactly what the prefix-matching price sheet
        # used to do to Gemini in production.
        est_cost = 0.0
        priced_any = False
        unpriced: dict[str, int] = {}
        for r in calls.values("model").annotate(
            n=Count("id"),
            prompt=Sum("prompt_tokens"),
            cached=Sum("cached_tokens"),
            cache_write=Sum("cache_write_tokens"),
            output=Sum("output_tokens"),
            thought=Sum("thought_tokens"),
            tool_prompt=Sum("tool_prompt_tokens"),
        ):
            model = r["model"] or ""
            c = _cost_usd(
                model,
                (r["prompt"] or 0) + (r["tool_prompt"] or 0),
                r["cached"] or 0,
                r["cache_write"] or 0,
                (r["output"] or 0) + (r["thought"] or 0),
            )
            if c is not None:
                est_cost += c
                priced_any = True
            else:
                unpriced[model] = r["n"] or 0

        catalog = getattr(settings, "LLM_CATALOG", None)
        rate_card = getattr(catalog, "rate_card", None)

        self.stdout.write("\n-- Totals --")
        self.stdout.write(f"  llm calls: {agg['n'] or 0}   total tokens: {total_tokens}")
        if priced_any:
            card = f"  [rate card {rate_card.version}]" if rate_card else ""
            self.stdout.write(
                self.style.NOTICE(
                    f"  estimated LLM-API cost: ~${est_cost:.2f}{card}  "
                    "(list-price estimate; EXCLUDES the fixed infra floor "
                    "that dominates the real bill — see LLM_SPEND_ANATOMY)"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING("  cost estimate unavailable: no priced model in window.")
            )
        if unpriced:
            listed = ", ".join(f"{m or '(blank)'} x{n}" for m, n in sorted(unpriced.items()))
            self.stdout.write(
                self.style.WARNING(
                    f"  UNPRICED, excluded from the total above: {listed}. "
                    f"Add each to apis/llm_models.yaml, or the cost above understates "
                    f"the window by however much these calls actually cost."
                )
            )

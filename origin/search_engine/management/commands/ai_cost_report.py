"""`python manage.py ai_cost_report` — the AI spend report and alarm.

Reads the `AiSpendEvent` / `AiRequestCost` ledger and answers the four
questions Phase 0 exists to answer:

    what did AI cost us, per provider, in the period?   (§ Providers)
    what is a request actually made of?                 (§ Purposes)
    what are we spending it on?                         (§ Surfaces)
    is anything escaping the meter?                     (§ Coverage)

    python manage.py ai_cost_report                  # last 7 days
    python manage.py ai_cost_report --days 30 --by-user
    python manage.py ai_cost_report --by-effort      # per-effort request anatomy
    python manage.py ai_cost_report --month          # calendar month to date
    python manage.py ai_cost_report --alert          # exit non-zero over budget
    python manage.py ai_cost_report --rebuild        # re-derive the rollups

WHY THIS IS THE ALARM, not a GCP budget. Claude and OpenAI bill outside
GCP entirely, so a `google_billing_budget` is structurally blind to two
of the three providers no matter how it is configured. A ledger-derived
threshold is the only cross-provider budget alarm that can exist here.
It is a `CronCommand`, so a breach exits non-zero and the Cloud Run job
goes red; `--email-to` additionally sends the summary, because a red job
nobody looks at is not an alarm.

WHY IT NEVER SUMS `AgentLlmCall`. That table is ask-path LATENCY
telemetry with `agent_run_metrics` on top; it covers only the agent loop
and only runs. This ledger covers every paid call on every surface. They
describe overlapping calls from two tables, and adding them would double
count. Neither supersedes the other.

RECONCILIATION is the point of the § Providers section: those USD totals
are what you compare against the Anthropic, OpenAI and GCP invoices.
Reconcile in USD — that is the currency the invoices are in and it
carries no FX assumption. JPY is the normalized internal view.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.search_engine.llm.spend import SURFACE_UNATTRIBUTED
from origin.search_engine.models import AiRequestCost, AiSpendEvent

log = logging.getLogger(__name__)


def _yen(milli: int) -> str:
    """Milli-yen -> a readable yen string."""
    return f"¥{milli / 1000:,.2f}"


def _usd(micro: int) -> str:
    return f"${micro / 1_000_000:,.4f}"


class Command(CronCommand):
    help = "Report internal AI spend from the cost ledger; optionally alert on a budget."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Lookback window (default 7).")
        parser.add_argument(
            "--month",
            action="store_true",
            help="Report the current UTC calendar month to date instead of --days.",
        )
        parser.add_argument("--by-user", action="store_true", help="Add a per-user breakdown.")
        parser.add_argument(
            "--by-effort",
            action="store_true",
            help=(
                "Add the per-effort request anatomy: purpose × effort with "
                "per-request means and the token buckets (cached share, "
                "thinking share) that size optimization levers."
            ),
        )
        parser.add_argument(
            "--alert",
            action="store_true",
            help=(
                "Exit non-zero when spend in the window exceeds "
                "AI_MONTHLY_BUDGET_JPY (scaled to the window), so the cron run "
                "goes red."
            ),
        )
        parser.add_argument(
            "--email-to",
            default="",
            help="Also send the summary to this address (uses the configured backend).",
        )
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help=(
                "Re-derive AiRequestCost rollups from AiSpendEvent for the "
                "window. Use after fixing a pricing bug — the events are "
                "ground truth, the rollup is derived."
            ),
        )

    # ---------------------------------------------------------------- #

    def handle(self, *args, **options):
        now = timezone.now()
        if options["month"]:
            cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            window = f"month to date ({cutoff:%Y-%m-%d} →)"
            window_days = max((now - cutoff).days, 1)
        else:
            days = max(1, int(options["days"]))
            cutoff = now - timedelta(days=days)
            window = f"last {days} day(s)"
            window_days = days

        events = AiSpendEvent.objects.filter(created_at__gte=cutoff)
        requests = AiRequestCost.objects.filter(started_at__gte=cutoff)

        if options["rebuild"]:
            self._rebuild(requests)

        self.stdout.write(f"=== AI cost — {window} ===")

        if not events.exists():
            self.stdout.write(
                self.style.WARNING(
                    "  No spend recorded in this window.\n"
                    "  If that is unexpected, AI_COST_METER is probably still off "
                    "— it defaults to false and collects nothing while off."
                )
            )
            return

        lines: list[str] = []
        total_jpy = self._totals_section(events, requests, lines)
        self._provider_section(events, lines)
        self._purpose_section(events, lines)
        if options["by_effort"]:
            self._effort_section(events, requests, lines)
        self._surface_section(events, lines)
        self._coverage_section(events, lines)
        if options["by_user"]:
            self._user_section(events, lines)

        for ln in lines:
            self.stdout.write(ln)

        if options["email_to"]:
            self._email(options["email_to"], window, lines)

        if options["alert"]:
            self._check_budget(total_jpy, window_days, window)

    # ---------------------------------------------------------------- #

    def _totals_section(self, events, requests, out: list[str]) -> int:
        agg = events.aggregate(
            n=Count("id"), jpy=Sum("cost_jpy_milli"), usd=Sum("cost_usd_micro")
        )
        total_jpy = int(agg["jpy"] or 0)
        req = requests.aggregate(
            n=Count("id"),
            ok=Count("id", filter=Q(result=AiRequestCost.RESULT_SUCCESS)),
            charged=Sum("charged_jpy_milli"),
            absorbed=Sum("absorbed_jpy_milli"),
        )
        n_ok = int(req["ok"] or 0)

        out.append("\n-- Totals --")
        out.append(f"  spend: {_yen(total_jpy)}  ({_usd(int(agg['usd'] or 0))})")
        out.append(f"  paid calls: {agg['n'] or 0}   logical requests: {req['n'] or 0}")
        if n_ok:
            out.append(f"  cost per SUCCESSFUL request: {_yen(total_jpy // max(n_ok, 1))}")
        # What we deliberately ate: failures, and anything over a quote.
        absorbed = int(req["absorbed"] or 0)
        if absorbed:
            out.append(
                f"  absorbed by us (failed requests + over-quote): {_yen(absorbed)}"
            )
        return total_jpy

    def _provider_section(self, events, out: list[str]) -> None:
        """The reconciliation table. USD, because invoices are in USD."""
        out.append("\n-- Providers (reconcile these against the invoices) --")
        out.append(f"  {'provider':<12} {'calls':>7} {'USD':>14} {'JPY':>14}")
        rows = events.values("provider").annotate(
            n=Count("id"), usd=Sum("cost_usd_micro"), jpy=Sum("cost_jpy_milli")
        )
        for r in sorted(rows, key=lambda x: -(x["usd"] or 0)):
            name = r["provider"] or "(unknown)"
            out.append(
                f"  {name:<12} {r['n']:>7} {_usd(int(r['usd'] or 0)):>14} "
                f"{_yen(int(r['jpy'] or 0)):>14}"
            )
        out.append(
            "  NOTE: Anthropic and OpenAI bill OUTSIDE GCP — the GCP console "
            "shows Gemini + embeddings only."
        )

    @staticmethod
    def _call_label(r: dict) -> str:
        """One display name per row kind.

        The purpose when tagged; otherwise the unit kind + provider, so
        Tavily/embedding/Cohere rows written before purpose tagging
        landed still get a real name instead of all folding into one
        `(untagged)` bucket that hides three different call types.
        """
        if r["purpose"]:
            return r["purpose"]
        if r["unit_kind"]:
            return f"({r['unit_kind']}:{r['provider'] or '?'})"
        return "(untagged)"

    def _purpose_section(self, events, out: list[str]) -> None:
        """What one request is actually made of."""
        out.append("\n-- Purposes (what a request is made of) --")
        out.append(f"  {'purpose':<16} {'calls':>7} {'JPY':>14}  share")
        rows = events.values("purpose", "unit_kind", "provider").annotate(
            n=Count("id"), jpy=Sum("cost_jpy_milli")
        )
        merged: dict[str, dict[str, int]] = {}
        for r in rows:
            m = merged.setdefault(self._call_label(r), {"n": 0, "jpy": 0})
            m["n"] += int(r["n"] or 0)
            m["jpy"] += int(r["jpy"] or 0)
        total = sum(m["jpy"] for m in merged.values()) or 1
        for label, m in sorted(merged.items(), key=lambda x: -x[1]["jpy"]):
            out.append(
                f"  {label:<16} {m['n']:>7} "
                f"{_yen(m['jpy']):>14}  {100 * m['jpy'] / total:4.1f}%"
            )

    _EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}

    def _effort_section(self, events, requests, out: list[str]) -> None:
        """The per-effort request anatomy: where each cent goes.

        This is the report the cost-optimization work steers by, so its
        columns are the levers: `in/req` + `cach%` say whether the
        provider's prompt cache is actually absorbing the re-sent
        prefix; `think/req` sizes what a thinking budget could reclaim;
        `c/req` × `JPY/req` per purpose ranks which call type to attack.

        Bucket semantics (normalized in the adapters): `in` = uncached +
        cached input; `cach%` = cached share of `in`. `think` is billed
        ON TOP of `out` on Gemini, reported as a SUBSET of `out` on
        OpenAI, and folded invisibly into `out` on Claude — so compare
        thinking within a provider, never across two.
        """
        out.append("\n-- By effort (request anatomy) --")

        # Requests per effort, for the per-request means. Falls back to
        # distinct request ids in the events when the rollup is missing
        # (e.g. a window whose requests closed outside it).
        n_req_by_effort = {
            (r["effort"] or ""): int(r["n"] or 0)
            for r in requests.values("effort").annotate(n=Count("id"))
        }
        fallback_req = {
            (r["effort"] or ""): int(r["n"] or 0)
            for r in events.values("effort").annotate(
                n=Count("request_id", distinct=True)
            )
        }

        rows = events.values("effort", "purpose", "unit_kind", "provider").annotate(
            n=Count("id"),
            jpy=Sum("cost_jpy_milli"),
            usd=Sum("cost_usd_micro"),
            prompt=Sum("prompt_tokens"),
            cached=Sum("cached_tokens"),
            outp=Sum("output_tokens"),
            thought=Sum("thought_tokens"),
            units=Sum("units"),
        )
        efforts: dict[str, dict[str, dict]] = {}
        for r in rows:
            eff = r["effort"] or "(none)"
            m = efforts.setdefault(eff, {}).setdefault(
                self._call_label(r),
                {
                    "n": 0, "jpy": 0, "usd": 0, "prompt": 0, "cached": 0,
                    "outp": 0, "thought": 0, "units": 0,
                    "unit_row": bool(r["unit_kind"]),
                },
            )
            for src, dst in (
                ("n", "n"), ("jpy", "jpy"), ("usd", "usd"), ("prompt", "prompt"),
                ("cached", "cached"), ("outp", "outp"), ("thought", "thought"),
                ("units", "units"),
            ):
                m[dst] += int(r[src] or 0)

        def _k(n: float) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}k"
            return f"{n:.0f}"

        for eff in sorted(efforts, key=lambda e: (self._EFFORT_ORDER.get(e, 9), e)):
            labels = efforts[eff]
            eff_key = "" if eff == "(none)" else eff
            n_req = n_req_by_effort.get(eff_key) or fallback_req.get(eff_key) or 1
            total_jpy = sum(m["jpy"] for m in labels.values())
            total_usd = sum(m["usd"] for m in labels.values())
            out.append(
                f"\n  == {eff} — {n_req} request(s), mean "
                f"{_yen(total_jpy // n_req)}/req ({_usd(total_usd // n_req)}) =="
            )
            out.append(
                f"    {'call':<16} {'calls':>6} {'c/req':>6} {'JPY/req':>9} "
                f"{'share':>6} {'in/req':>8} {'cach%':>6} {'out/req':>8} {'think/req':>9}"
            )
            for label, m in sorted(labels.items(), key=lambda x: -x[1]["jpy"]):
                base = (
                    f"    {label:<16} {m['n']:>6} {m['n'] / n_req:>6.1f} "
                    f"{_yen(m['jpy'] // n_req):>9} "
                    f"{100 * m['jpy'] / (total_jpy or 1):>5.1f}%"
                )
                if m["unit_row"]:
                    out.append(f"{base}  [{m['units']} unit(s)]")
                    continue
                total_in = m["prompt"] + m["cached"]
                cach_pct = 100 * m["cached"] / total_in if total_in else 0.0
                out.append(
                    f"{base} {_k(total_in / n_req):>8} {cach_pct:>5.0f}% "
                    f"{_k(m['outp'] / n_req):>8} {_k(m['thought'] / n_req):>9}"
                )

    def _surface_section(self, events, out: list[str]) -> None:
        out.append("\n-- Surfaces --")
        rows = events.values("surface").annotate(n=Count("id"), jpy=Sum("cost_jpy_milli"))
        for r in sorted(rows, key=lambda x: -(x["jpy"] or 0)):
            out.append(
                f"  {(r['surface'] or '(none)'):<16} {r['n']:>7} calls  "
                f"{_yen(int(r['jpy'] or 0)):>14}"
            )

    def _coverage_section(self, events, out: list[str]) -> None:
        """Is anything escaping the meter, and is the total trustworthy?

        Three ways the number above can be wrong, each named rather than
        quietly folded in.
        """
        out.append("\n-- Coverage --")

        unattributed = events.filter(surface=SURFACE_UNATTRIBUTED)
        n_unattr = unattributed.count()
        if n_unattr:
            by_purpose = ", ".join(
                f"{r['purpose'] or '(none)'} x{r['n']}"
                for r in unattributed.values("purpose").annotate(n=Count("id"))
            )
            out.append(
                self.style.WARNING(
                    f"  UNATTRIBUTED: {n_unattr} call(s) with no request context "
                    f"[{by_purpose}]. An entry point is missing a spend_context() "
                    f"bind — this is the tripwire for a new uninstrumented path."
                )
            )
        else:
            out.append("  unattributed: none — every paid call had a request context.")

        unpriced = events.filter(cost_basis="unpriced")
        if unpriced.exists():
            by_kind = ", ".join(
                f"{r['provider'] or '?'}/{r['unit_kind'] or 'tokens'} "
                f"x{r['n']} ({r['units'] or 0} units)"
                for r in unpriced.values("provider", "unit_kind").annotate(
                    n=Count("id"), units=Sum("units")
                )
            )
            out.append(
                f"  unpriced (NOT in the totals above): {by_kind}. "
                f"Every billed line has a rate now — embeddings under "
                f"`embeddings:`, web search and rerank under `unit_prices:` "
                f"in llm_models.yaml — so anything here means a MISSING "
                f"entry for that exact model/unit, not a policy."
            )

        incomplete = events.filter(cost_basis="incomplete").count()
        if incomplete:
            out.append(
                f"  incomplete: {incomplete} call(s) died before the provider "
                f"reported usage — billed, but we cannot size them."
            )

        cards = list(events.values_list("rate_card_version", flat=True).distinct())
        if len(cards) > 1:
            out.append(
                self.style.WARNING(
                    f"  {len(cards)} RATE CARDS in this window: {', '.join(sorted(cards))}. "
                    f"Prices changed mid-period, so the total spans two regimes — "
                    f"re-run per card before drawing a trend from it."
                )
            )

    def _user_section(self, events, out: list[str]) -> None:
        out.append("\n-- By user --")
        rows = events.values("user_id").annotate(n=Count("id"), jpy=Sum("cost_jpy_milli"))
        for r in sorted(rows, key=lambda x: -(x["jpy"] or 0))[:20]:
            out.append(
                f"  {(r['user_id'] or '(none)'):<40} {r['n']:>6} calls  "
                f"{_yen(int(r['jpy'] or 0)):>14}"
            )

    # ---------------------------------------------------------------- #

    def _rebuild(self, requests) -> None:
        """Re-derive rollups from the events.

        The events are ground truth; the rollup is derived. This is what
        makes a pricing bug fixable after the fact instead of baked in.
        """
        from origin.search_engine import spend_recorder  # noqa: PLC0415
        from origin.search_engine.llm import spend  # noqa: PLC0415

        n = 0
        for row in requests.iterator():
            ctx = spend.SpendContext(
                request_id=str(row.request_id),
                surface=row.surface,
                user_id=row.user_id,
                team_id=row.team_id or "",
                plan=row.plan,
                effort=row.effort,
                run_id=str(row.run_id) if row.run_id else None,
            )
            spend_recorder.close_request(ctx, result=row.result or AiRequestCost.RESULT_SUCCESS)
            n += 1
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {n} request rollup(s) from events."))

    def _email(self, to: str, window: str, lines: list[str]) -> None:
        """Send the summary. A red cron job nobody looks at is not an alarm."""
        try:
            from origin.services.email import send_templated_email  # noqa: PLC0415

            send_templated_email(
                to=to,
                subject=f"Genos AI spend — {window}",
                template_base="ai_cost_report",
                context={"window": window, "body": "\n".join(lines)},
            )
            self.stdout.write(f"\nEmailed the summary to {to}.")
        except Exception:  # noqa: BLE001
            # WARNING, not ERROR: a mail outage must not red a cron run
            # whose actual job — reporting — succeeded.
            log.warning("Could not email the AI cost report to %s", to, exc_info=True)
            self.stdout.write(self.style.WARNING(f"\nCould not email the summary to {to}."))

    def _check_budget(self, total_jpy_milli: int, window_days: int, window: str) -> None:
        """Compare the window against the monthly budget, pro-rated.

        Logs at ERROR so `CronCommand`'s tripwire fails the run — that is
        the signal. Budget 0 (the default) means no budget is configured
        and nothing is checked, so this stays quiet until an operator
        opts in.
        """
        budget_month = float(settings.SEARCH_ENGINE.get("AI_MONTHLY_BUDGET_JPY", 0) or 0)
        if budget_month <= 0:
            self.stdout.write(
                "\n  (no AI_MONTHLY_BUDGET_JPY configured — nothing to alert on)"
            )
            return

        allowed_milli = int(budget_month * 1000 * (window_days / 30.0))
        spent = total_jpy_milli
        pct = 100 * spent / max(allowed_milli, 1)
        self.stdout.write(
            f"\n-- Budget --\n"
            f"  {_yen(spent)} of {_yen(allowed_milli)} pro-rated for {window} ({pct:.0f}%)"
        )
        if spent > allowed_milli:
            log.error(
                "AI spend %s exceeded the pro-rated budget %s for %s (%.0f%%)",
                _yen(spent),
                _yen(allowed_milli),
                window,
                pct,
            )
        elif pct >= 50:
            # The V2 §3.7 early warnings, at 50% and 80%. WARNING, not
            # ERROR — the cron must stay green below the line, or the
            # red run stops meaning "over budget". The graded messages
            # exist so the first sign of a hot month is a log line and
            # an email, not the alarm itself.
            level = "80%" if pct >= 80 else "50%"
            self.stdout.write(
                self.style.WARNING(
                    f"  ⚠ over the {level} early-warning threshold of the "
                    f"pro-rated budget"
                )
            )
            log.warning(
                "AI spend %s is at %.0f%% of the pro-rated budget %s for %s "
                "(early warning, over the %s line)",
                _yen(spent),
                pct,
                _yen(allowed_milli),
                window,
                level,
            )

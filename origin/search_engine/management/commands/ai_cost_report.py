"""`python manage.py ai_cost_report` — the AI spend report and alarm.

Reads the `AiSpendEvent` / `AiRequestCost` ledger and answers the four
questions Phase 0 exists to answer:

    what did AI cost us, per provider, in the period?   (§ Providers)
    what is a request actually made of?                 (§ Purposes)
    what are we spending it on?                         (§ Surfaces)
    is anything escaping the meter?                     (§ Coverage)

    python manage.py ai_cost_report                  # last 7 days
    python manage.py ai_cost_report --days 30 --by-user
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

    def _purpose_section(self, events, out: list[str]) -> None:
        """What one request is actually made of."""
        out.append("\n-- Purposes (what a request is made of) --")
        out.append(f"  {'purpose':<14} {'calls':>7} {'JPY':>14}  share")
        rows = list(
            events.values("purpose").annotate(n=Count("id"), jpy=Sum("cost_jpy_milli"))
        )
        total = sum(int(r["jpy"] or 0) for r in rows) or 1
        for r in sorted(rows, key=lambda x: -(x["jpy"] or 0)):
            jpy = int(r["jpy"] or 0)
            out.append(
                f"  {(r['purpose'] or '(untagged)'):<14} {r['n']:>7} "
                f"{_yen(jpy):>14}  {100 * jpy / total:4.1f}%"
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
                f"Web search and rerank bill per unit with no rate in the "
                f"catalog, and reconcile against their own invoices. "
                f"Embeddings ARE priced — if they appear here, the model has "
                f"no entry under `embeddings:` in llm_models.yaml."
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

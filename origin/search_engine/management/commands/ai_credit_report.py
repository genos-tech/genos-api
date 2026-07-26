"""`python manage.py ai_credit_report` — what shadow credits would have done.

The output Phase 1 exists to produce: under 10/40/100/200 at ¥15 per
credit, who would have run out, and when in the month. Everything else
in the credit engine is machinery for making this page trustworthy.

    python manage.py ai_credit_report                    # this UTC month
    python manage.py ai_credit_report --period 2026-06   # a closed month
    python manage.py ai_credit_report --compare-policy /path/to/candidate.yaml

Plain BaseCommand, deliberately not CronCommand: `ai_cost_report` is
the alarm (its exit code pages); this is the ANALYSIS page, and a
rendering change here must not be able to red a cron. Same split as
the cost dashboard, same reason.

`--compare-policy` is V2 §5.1's dual calculation: replay every stored
request under a CANDIDATE policy file and diff the outcome against
what was actually posted. It can only exist because the credit math is
pure functions over stored inputs — nothing here writes anything, and
the posted ledger is never touched.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum
from django.utils import timezone

from origin.search_engine import credit_ledger, credits
from origin.search_engine.models import AiCreditEntry, AiRequestCost

# Allowance milestones reported per plan (V2 §8.3).
_MILESTONES = (50, 80, 100)


def _cr(milli: int | float) -> str:
    return f"{milli / 1000:,.2f}cr"


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "–"


def _percentiles(values: list[int], points=(50, 75, 90, 95)) -> dict[str, int]:
    """Nearest-rank percentiles. Tiny-n honest: with 3 requests P95 IS
    the max, and pretending otherwise would be precision theater."""
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for p in points:
        rank = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered)) - 1))
        out[f"p{p}"] = ordered[rank]
    out["max"] = ordered[-1]
    return out


class Command(BaseCommand):
    help = "Report what shadow credits would have done to each plan this period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period",
            default=None,
            help="UTC month as YYYY-MM (default: the current month to date).",
        )
        parser.add_argument(
            "--compare-policy",
            dest="compare_policy",
            default=None,
            help=(
                "Path to a CANDIDATE credit_policy.yaml. Replays every stored "
                "request in the window under it and diffs against what was "
                "posted. Read-only — the ledger is never touched."
            ),
        )

    def handle(self, *args, **options):
        period = options.get("period") or credit_ledger.period_for()
        try:
            year, month = (int(x) for x in period.split("-"))
            start = timezone.now().replace(
                year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        except (ValueError, TypeError):
            raise CommandError(f"--period must be YYYY-MM, got {period!r}")
        # Exclusive end — the first instant of the next month.
        end = (
            start.replace(year=year + 1, month=1) if month == 12 else start.replace(month=month + 1)
        )

        requests = AiRequestCost.objects.filter(started_at__gte=start, started_at__lt=end)
        entries = AiCreditEntry.objects.filter(period=period)

        self.stdout.write(f"=== AI credits (shadow) — {period} ===")
        if not entries.exists() and not requests.filter(shadow_credits_milli__gt=0).exists():
            self.stdout.write(
                self.style.WARNING(
                    "  No shadow-credit data in this period.\n"
                    "  AI_CREDITS_SHADOW is probably off (it defaults to false, and\n"
                    "  requires AI_COST_METER). Nothing is charged either way — this\n"
                    "  report only ever describes what WOULD have happened."
                )
            )
            return

        self._policy_regime_section(requests)
        self._per_plan_section(requests, entries, period)
        self._request_shape_section(requests)
        self._divergence_section(requests)

        if options.get("compare_policy"):
            self._compare_policy(requests, options["compare_policy"])

    # ------------------------------------------------------------------ #

    def _policy_regime_section(self, requests) -> None:
        """Flag a window spanning more than one credit policy.

        The cost report already refuses to sum across `rate_card_version`
        boundaries without saying so — "two price regimes are not one
        trend". The same rule has to hold for the CONVERSION policy, and
        it did not: this section exists because the first real shadow
        window mixed a Phase 0 `shadow-v0` row (¥2/credit, and no
        `eligible` column at all — it defaulted to 0) with `cp-v1` rows
        at ¥15/credit. The totals below added them, so the divergence
        line read "eligible ¥0.00, credited ¥12.68", which cannot be
        true of any single regime.

        Rows from a superseded policy are still HISTORY and are never
        rewritten (§3.6). They just must not be averaged with the
        current one silently.
        """
        versions = sorted(
            v
            for v in requests.values_list("credit_policy_version", flat=True).distinct()
            if v
        )
        if len(versions) <= 1:
            return
        from django.conf import settings  # noqa: PLC0415

        current = settings.CREDIT_POLICY.version
        stale = [v for v in versions if v != current]
        self.stdout.write(
            self.style.WARNING(
                f"\n⚠ MULTIPLE CREDIT POLICIES IN THIS WINDOW: {', '.join(versions)}.\n"
                f"  Every figure below sums across them. Rows under "
                f"{', '.join(stale)} were charged by a DIFFERENT conversion and\n"
                f"  are not comparable with {current} — a per-request credit "
                f"figure spanning two policies is two answers added together.\n"
                f"  Compare like with like using --period on a window inside one "
                f"regime."
            )
        )

    def _per_plan_section(self, requests, entries, period: str) -> None:
        """The headline: per plan, who would have run out, and when."""
        from django.conf import settings  # noqa: PLC0415

        policy = settings.CREDIT_POLICY
        self.stdout.write("\n-- Plans (the Phase 1 question) --")
        self.stdout.write(
            f"  {'plan':<12}{'users':>6}{'charged':>12}{'≥50%':>6}{'≥80%':>6}"
            f"{'≥100%':>7}{'blocked-asks':>14}"
        )

        # Charges by (user, plan-entitlement) — consumption is a ledger
        # fact; the entitlement compared against is the plan's grant.
        charges_by_user: dict[str, int] = defaultdict(int)
        for row in (
            entries.filter(entry_type=AiCreditEntry.ENTRY_CHARGE)
            .values("user_id")
            .annotate(s=Sum("credits_milli"))
        ):
            charges_by_user[row["user_id"]] = -int(row["s"] or 0)

        plan_by_user: dict[str, str] = {}
        for row in requests.exclude(user_id="").values("user_id", "plan"):
            # Last write wins; plans can move mid-month and the report
            # judges against the best plan seen — customer-favorable.
            best = plan_by_user.get(row["user_id"], "free")
            if (policy.entitlements_milli.get(row["plan"]) or 0) >= (
                policy.entitlements_milli.get(best) or 0
            ):
                plan_by_user[row["user_id"]] = row["plan"] or "free"

        blocked_by_plan: dict[str, int] = defaultdict(int)
        for row in requests.filter(would_have_blocked=True).values("plan").annotate(n=Count("id")):
            blocked_by_plan[row["plan"] or "free"] += int(row["n"] or 0)

        plans: dict[str, dict] = defaultdict(lambda: {"users": 0, "charged": 0, 50: 0, 80: 0, 100: 0})
        exhausted_when: list[str] = []
        for user_id, charged in charges_by_user.items():
            plan = plan_by_user.get(user_id, "free")
            entitlement = policy.entitlements_milli.get(plan)
            bucket = plans[plan]
            bucket["users"] += 1
            bucket["charged"] += charged
            if entitlement:
                for m in _MILESTONES:
                    if charged >= entitlement * m / 100:
                        bucket[m] += 1
                if charged >= entitlement:
                    exhausted_when.append(
                        self._exhaustion_line(user_id, plan, entitlement, period)
                    )

        for plan in ("free", "core", "pro", "max", "enterprise"):
            if plan not in plans:
                continue
            b = plans[plan]
            self.stdout.write(
                f"  {plan:<12}{b['users']:>6}{_cr(b['charged']):>12}"
                f"{b[50]:>6}{b[80]:>6}{b[100]:>7}{blocked_by_plan.get(plan, 0):>14}"
            )
        self.stdout.write(
            "  `blocked-asks` counts requests whose opening balance could not cover\n"
            "  the quoted max — what an AUTHORITATIVE system would have refused."
        )
        if exhausted_when:
            self.stdout.write("\n  Exhaustion (day of month the allowance ran out):")
            for line in exhausted_when:
                self.stdout.write(f"    {line}")

    def _exhaustion_line(self, user_id: str, plan: str, entitlement: int, period: str) -> str:
        """The DAY this user's cumulative charges crossed the allowance
        — 'a customer hitting a wall in week three' made visible before
        any customer can (V1 §6.4's warning, answered with data)."""
        running = 0
        for entry in (
            AiCreditEntry.objects.filter(
                user_id=user_id, period=period, entry_type=AiCreditEntry.ENTRY_CHARGE
            ).order_by("created_at")
        ):
            running += -entry.credits_milli
            if running >= entitlement:
                return f"user {user_id} ({plan}): day {entry.created_at.day}"
        return f"user {user_id} ({plan}): (over by grants/reversals)"

    def _request_shape_section(self, requests) -> None:
        """Percentiles of the billable request — the V2 §8.3 shape
        metrics, and the input the eventual per-class quote needs."""
        billable = requests.filter(
            result=AiRequestCost.RESULT_SUCCESS, shadow_credits_milli__gt=0
        )
        credits_list = list(billable.values_list("shadow_credits_milli", flat=True))
        self.stdout.write("\n-- Billable request shape --")
        if not credits_list:
            self.stdout.write("  (no billable requests in this period)")
            return
        pct = _percentiles(credits_list)
        self.stdout.write(
            "  credits/request  "
            + "  ".join(f"{k}={_cr(v)}" for k, v in pct.items())
            + f"  (n={len(credits_list)})"
        )
        total_credits = sum(credits_list)
        self.stdout.write(
            f"  requests per 100 credits: "
            f"{100_000 * len(credits_list) / total_credits:,.0f}"
            if total_credits
            else "  requests per 100 credits: –"
        )

    def _divergence_section(self, requests) -> None:
        """Credit-vs-yen divergence (V2 §8.2): how much REAL yen sits
        behind each credited yen. 1.0 = credits track cost exactly;
        above it, we absorb (failures, caps, non-billable surfaces)."""
        agg = requests.aggregate(
            real=Sum("computed_jpy_milli"),
            eligible=Sum("eligible_jpy_milli"),
            credited=Sum("shadow_credits_milli"),
        )
        from django.conf import settings  # noqa: PLC0415

        credited_jpy = int((agg["credited"] or 0) * settings.CREDIT_POLICY.credit_jpy)
        real = int(agg["real"] or 0)
        self.stdout.write("\n-- Divergence (real cost vs what credits would carry) --")
        self.stdout.write(
            f"  actual cost ¥{real / 1000:,.2f}   eligible ¥{(agg['eligible'] or 0) / 1000:,.2f}"
            f"   credited ≈¥{credited_jpy / 1000:,.2f}"
        )
        if credited_jpy:
            self.stdout.write(
                f"  absorption ratio: {real / credited_jpy:,.2f}× "
                f"(real yen per credited yen — failures, caps and internal "
                f"surfaces are ours)"
            )

    # ------------------------------------------------------------------ #

    def _compare_policy(self, requests, candidate_path: str) -> None:
        """V2 §5.1 dual calculation. Replays the PURE functions over
        stored rollups under the candidate; posted history untouched."""
        from apis.credit_policy import CreditPolicyError, load_credit_policy  # noqa: PLC0415

        try:
            candidate = load_credit_policy(candidate_path)
        except CreditPolicyError as e:
            raise CommandError(str(e))

        self.stdout.write(
            f"\n-- Dual calculation: posted vs candidate {candidate.version} --"
        )
        rows = list(
            requests.values(
                "result", "surface", "computed_jpy_milli", "shadow_credits_milli", "plan"
            )
        )
        posted_total = 0
        candidate_total = 0
        candidate_list: list[int] = []
        by_plan: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            posted_total += int(r["shadow_credits_milli"] or 0)
            eligible = credits.eligible_jpy_milli(
                result=r["result"],
                surface=r["surface"],
                computed_jpy_milli=int(r["computed_jpy_milli"] or 0),
                policy=candidate,
            )
            c = credits.credits_milli(eligible, candidate)
            candidate_total += c
            if c > 0:
                candidate_list.append(c)
                by_plan[r["plan"] or "free"].append(c)

        self.stdout.write(
            f"  total charged:  posted {_cr(posted_total)}  →  candidate {_cr(candidate_total)}"
            + (
                f"  ({100 * (candidate_total - posted_total) / posted_total:+.0f}%)"
                if posted_total
                else ""
            )
        )
        pct = _percentiles(candidate_list)
        if pct:
            self.stdout.write(
                "  candidate credits/request  " + "  ".join(f"{k}={_cr(v)}" for k, v in pct.items())
            )
        for plan, values in sorted(by_plan.items()):
            entitlement = candidate.entitlements_milli.get(plan)
            month_total = sum(values)
            share = (
                f"{100 * month_total / entitlement:.0f}% of {plan}'s allowance"
                if entitlement
                else "unlimited plan"
            )
            self.stdout.write(f"  {plan:<12}{_cr(month_total):>12}  ({share})")
        self.stdout.write(
            "  Read-only replay — nothing was posted, and §5.2's purchasing-power\n"
            "  guideposts apply before this candidate ever goes live."
        )

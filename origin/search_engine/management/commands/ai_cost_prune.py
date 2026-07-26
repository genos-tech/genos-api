"""`python manage.py ai_cost_prune` — event retention, with hard refusals.

Roughly 10–40 `AiSpendEvent` rows accrue per ask. This prunes old
events — and ONLY events. Three rules are structural, not options:

  1. Never inside the reconciliation window (`--days`, default 90 —
     two invoice cycles of headroom). Events are the only thing an
     invoice can be reconciled against.
  2. Never events whose request has NO rollup: the rollup is the
     derived summary that survives; pruning unrolled events would
     delete the only record those calls happened.
  3. Never events whose request has a POSTED CREDIT CHARGE. `--rebuild`
     re-derives eligible cost from events; pruning a charged request's
     events would make any future rebuild derive a different
     `eligible_jpy_milli` than the charge was posted against, and
     nothing would ever surface the divergence.

`AiRequestCost` rollups and `AiCreditEntry` rows are never touched by
this command at all — rollups are the long-lived summary, and the
ledger is append-only history.

    python manage.py ai_cost_prune --dry-run       # count only
    python manage.py ai_cost_prune                 # delete (90-day window)
    python manage.py ai_cost_prune --days 180
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.search_engine.models import AiCreditEntry, AiRequestCost, AiSpendEvent

_MIN_DAYS = 30


class Command(BaseCommand):
    help = "Prune old AiSpendEvent rows (rollups and credit entries are never touched)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help=(
                "Keep events newer than this many days (default 90 — two "
                f"invoice cycles). Refuses anything under {_MIN_DAYS}."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting.",
        )

    def handle(self, *args, **options):
        days = int(options["days"])
        if days < _MIN_DAYS:
            raise CommandError(
                f"--days {days} is inside the reconciliation window; the minimum "
                f"is {_MIN_DAYS}. Events are the only thing an invoice can be "
                f"reconciled against."
            )
        cutoff = timezone.now() - timedelta(days=days)

        old = AiSpendEvent.objects.filter(created_at__lt=cutoff)
        total_old = old.count()

        rolled_ids = AiRequestCost.objects.values_list("request_id", flat=True)
        charged_ids = AiCreditEntry.objects.filter(
            entry_type=AiCreditEntry.ENTRY_CHARGE
        ).values_list("request_id", flat=True)

        prunable = old.filter(request_id__in=rolled_ids).exclude(request_id__in=charged_ids)
        n_prunable = prunable.count()

        kept_unrolled = old.exclude(request_id__in=rolled_ids).count()
        kept_charged = old.filter(request_id__in=charged_ids).count()

        self.stdout.write(
            f"=== ai_cost_prune — events older than {days}d "
            f"(before {cutoff:%Y-%m-%d}) ===\n"
            f"  old events:            {total_old:>8}\n"
            f"  prunable:              {n_prunable:>8}\n"
            f"  kept (no rollup):      {kept_unrolled:>8}  — the only record those calls happened\n"
            f"  kept (charge posted):  {kept_charged:>8}  — a rebuild must still derive the "
            f"eligible cost the charge was posted against"
        )

        if options.get("dry_run"):
            self.stdout.write(self.style.NOTICE("  --dry-run: nothing deleted."))
            return

        deleted, _ = prunable.delete()
        self.stdout.write(self.style.SUCCESS(f"  deleted {deleted} event row(s)."))

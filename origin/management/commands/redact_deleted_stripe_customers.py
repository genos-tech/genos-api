"""`python manage.py redact_deleted_stripe_customers` - clear the Stripe name and email of an
account deleted more than 7 years ago (MIP-2252, see `services/stripe_retention`).

    python manage.py redact_deleted_stripe_customers            # DRY RUN: a count, writes nothing
    python manage.py redact_deleted_stripe_customers --apply    # redact (at most --limit per run)

Output is COUNTS ONLY. Safe to re-run. Not scheduled: nothing is old enough before 2033.
"""

from __future__ import annotations

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.services import stripe_retention as service
from origin.services.stripe_billing import BillingError


class Command(CronCommand):
    help = (
        "Count (dry run, the default) or redact (--apply) the Stripe customer of an account "
        "deleted past the 7-year bookkeeping period. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Redact. Default: dry run.")
        parser.add_argument("--limit", type=int, default=service.DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("redact_deleted_stripe_customers: " + ("APPLY" if apply else "DRY RUN"))
        try:
            out = service.redact(apply=apply, limit=int(opts["limit"]))
        except BillingError as exc:
            raise CommandError(f"Stripe is not available: {exc}") from exc
        if apply:
            self.stdout.write(
                f"redacted {out.redacted}, already gone at Stripe {out.gone}, "
                f"skipped (still billed or a recent invoice) {out.skipped}, "
                f"failed (left for the next run) {out.failed}"
            )
        else:
            self.stdout.write(f"would redact {out.matched} customers")

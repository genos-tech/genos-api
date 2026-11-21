"""`python manage.py sync_team_stripe_emails` - move team Stripe customers to the owner's email (MIP-1904).

Team Stripe customers used to be created with `team.team_email`, a generated `...@genos.tech`
address on a domain Genos does not own: receipts, invoices and payment-failure mails for the
team's subscription went nowhere, or to a third party. New customers now get the team OWNER's own
email (`stripe_billing.team_billing_email`). This updates the existing ones.

DRY RUN IS THE DEFAULT: it reads the database and counts, and calls nothing at Stripe. `--apply`
needs `--yes` and writes to Stripe (`Customer.modify(email=...)`), so it is a PRODUCTION STRIPE
WRITE: run it only on the owner's go. Safe to run twice (it sets the same value again). Output is
COUNTS ONLY: no email, no customer id, no team name.

    python manage.py sync_team_stripe_emails                  # counts only
    python manage.py sync_team_stripe_emails --apply --yes
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from origin.models.common.team_models import TeamMaster
from origin.services import stripe_billing

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Set the Stripe customer email of every team with a billing account to its owner's own "
        "email. Dry run by default (database only); --apply --yes writes to Stripe. Counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Update Stripe. Default: count only."
        )
        parser.add_argument("--yes", action="store_true", help="Confirm --apply (a Stripe write).")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply writes to Stripe: add --yes.")

        teams = (
            TeamMaster.objects.filter(is_deleted=False)
            .exclude(stripe_customer_id__isnull=True)
            .exclude(stripe_customer_id="")
            .select_related("owner")
        )
        total = skipped = updated = failed = 0
        todo = []
        for team in teams:
            total += 1
            if not stripe_billing.team_billing_email(team):
                skipped += 1
                continue
            todo.append(team)

        if apply_changes:
            stripe = stripe_billing._stripe()
            for team in todo:
                try:
                    stripe.Customer.modify(
                        team.stripe_customer_id, email=stripe_billing.team_billing_email(team)
                    )
                except Exception:  # noqa: BLE001 - never an email or id in the log
                    log.warning("sync_team_stripe_emails: one customer could not be updated")
                    failed += 1
                else:
                    updated += 1

        say = self.stdout.write
        say("sync_team_stripe_emails: " + ("APPLY" if apply_changes else "DRY RUN"))
        say(f"  {total} team(s) with a Stripe customer")
        say(f"  {skipped} skipped (the owner has no email)")
        if apply_changes:
            say(f"  {updated} customer(s) updated at Stripe")
            if failed:
                say(self.style.WARNING(f"  {failed} customer(s) could not be updated"))
        else:
            say(f"  {len(todo)} would be updated by --apply --yes")

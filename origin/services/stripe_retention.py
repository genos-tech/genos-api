"""Redact a deleted account's Stripe customer after the bookkeeping period (MIP-2252, D5).

Account deletion keeps the person's Stripe customer (Ken, 2026-11-02, MIP-874): invoices and
payments must stay readable for the tax records. What Stripe holds about the person is a name
and an email address. Japanese bookkeeping rules keep the records for 7 years, so after
`BOOKKEEPING_DAYS` the customer is **redacted** (name and email cleared at Stripe) and our row
forgets the customer id. The customer, its invoices and its payments are not deleted: only the
two personal fields are.

  * The age is `CustomUser.ts_updated_at` of an account that is `is_deleted`. Deletion is the
    last write to the row, so it is the deletion time; any later write only moves it later,
    which delays the redaction and never brings it forward.
  * The period is a floor, not a setting: there is no variable that shortens it. A customer
    that still has a subscription that is not cancelled, or an invoice newer than the period,
    is skipped (and stays a candidate), so a later invoice keeps its own 7 years.
  * What is cleared is the Customer object's `name`, `email` and `phone`. NOT cleared: the
    billing details on a payment method attached to the customer (Checkout fills a cardholder
    name, email and address there), and a customer `address` or `shipping`. Those stay with
    Stripe's own retention until someone decides to detach the payment methods.
  * Redacted means the local `stripe_customer_id` is cleared, so the row is not found again
    (no extra column, no migration). A customer Stripe says is gone (`resource_missing`) is
    treated the same way. Any other Stripe failure leaves the row for the next run.
  * Dry run counts; counts only, never an id, a name or an email.
  * Teams are not handled here: a closed team's customer holds a billing contact, and
    `team_purge` keeps it only while a paid plan could still bill. Tracked separately.

Not scheduled: nothing can be old enough before 2033 at the earliest. Schedule the command with
a `--heartbeat` when that is near.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from origin.models.common.user_models import CustomUser

log = logging.getLogger("origin.retention")

# 7 years, with the two leap days, in days.
BOOKKEEPING_DAYS = 7 * 365 + 2
DEFAULT_LIMIT = 200


@dataclass
class Outcome:
    matched: int = 0
    redacted: int = 0
    gone: int = 0
    skipped: int = 0  # still billed, or an invoice inside the period: left for a later run
    failed: int = 0
    dry_run: bool = True


def candidates(now: datetime | None = None) -> QuerySet:
    """Deleted accounts past the bookkeeping period that still point at a Stripe customer."""
    cutoff = (now or timezone.now()) - timedelta(days=BOOKKEEPING_DAYS)
    return (
        CustomUser.objects.filter(is_deleted=True, ts_updated_at__lt=cutoff)
        .exclude(Q(stripe_customer_id__isnull=True) | Q(stripe_customer_id=""))
        .order_by("ts_updated_at", "id")
    )


def _still_billed_or_recent(stripe, customer_id: str, cutoff: datetime) -> bool:
    """True when the customer must be left alone for now: a subscription that is not
    cancelled (it may still bill, and account deletion does not cancel it: MIP-2254), or an
    invoice newer than the cutoff (its own 7 years have not run). Counted from deletion alone,
    the period could end inside the bookkeeping period of a later invoice."""
    subs = stripe.Subscription.list(customer=customer_id, status="all", limit=100)
    if any(getattr(s, "status", None) != "canceled" for s in subs.data):
        return True
    invoices = stripe.Invoice.list(customer=customer_id, limit=1)
    return any(
        datetime.fromtimestamp(inv.created, tz=cutoff.tzinfo) >= cutoff for inv in invoices.data
    )


def redact(
    *, apply: bool = False, limit: int = DEFAULT_LIMIT, now: datetime | None = None
) -> Outcome:
    qs = candidates(now)
    if not apply:
        return Outcome(matched=qs.count(), dry_run=True)

    from origin.services import stripe_billing

    stripe = stripe_billing._stripe()  # BillingError when Stripe is not configured
    out = Outcome(dry_run=False)
    cutoff = (now or timezone.now()) - timedelta(days=BOOKKEEPING_DAYS)
    for pk, customer_id in list(qs.values_list("pk", "stripe_customer_id")[:limit]):
        out.matched += 1
        try:
            if _still_billed_or_recent(stripe, customer_id, cutoff):
                out.skipped += 1
                continue
            # An empty string unsets the field at Stripe.
            stripe.Customer.modify(customer_id, name="", email="", phone="")
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", "") == "resource_missing":
                out.gone += 1
            else:
                out.failed += 1
                # The type only: a Stripe error message can quote the customer.
                log.warning("stripe redaction failed: %s", type(exc).__name__)
                continue
        else:
            out.redacted += 1
        # `update`, not `save`: the age above must not move.
        CustomUser.objects.filter(pk=pk).update(stripe_customer_id=None)
    if out.failed:
        # ERROR makes the cron run red (`CronCommand`); the rows stay for the next run.
        log.error("stripe redaction: %d customers failed", out.failed)
    return out

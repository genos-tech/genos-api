"""Two credit buckets: the monthly allowance, and what was bought.

The rules the product sold, and what makes each expensive if wrong:

  1. **Monthly first.** Purchased credits are only touched once the
     month's allowance is gone. Get this backwards and a customer's
     non-expiring credits burn while their expiring ones go to waste —
     the one outcome nobody would knowingly buy.
  2. **Purchased credits never expire.** The monthly bucket empties at
     the period rollover; the purchased one carries. This is the whole
     value proposition, and the ledger's expiry model is a `period`
     column, so it takes work to NOT expire something.
  3. **Enforcement still reads one number.** The gate, the budget and
     the mid-run stop all take a scalar; splitting the balance must not
     change what any of them see.

The split is DERIVED — no charge row records which bucket it drew from,
because `uq_credit_charge_per_request` allows one charge per request and
`post_charge` takes no `kind`. These tests are what makes that derivation
trustworthy.
"""

from __future__ import annotations

import uuid

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from origin.search_engine import credit_ledger
from origin.search_engine.models import AiCreditEntry

USER = "buyer-1"
# credit_policy.yaml: pro = 70 credits = 70_000 milli.
PRO_MILLI = 70_000


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


def _last_period() -> str:
    now = credit_ledger.period_for()
    year, month = (int(p) for p in now.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


class BucketTestCase(TestCase):
    """The balance is cached per (user, period) in shared locmem, which
    outlives a test — the DB rolls back and the cache does not."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    # -- posting helpers, at the shapes production actually writes --

    def grant_monthly(self, milli=PRO_MILLI, *, plan="pro", period=None):
        return AiCreditEntry.objects.create(
            user_id=USER,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_MONTHLY,
            credits_milli=milli,
            period=period or credit_ledger.period_for(),
            plan=plan,
            actor="system",
        )

    def grant_purchased(self, milli, *, period=None):
        return AiCreditEntry.objects.create(
            user_id=USER,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_PURCHASED,
            credits_milli=milli,
            period=period or credit_ledger.period_for(),
            plan="pro",
            actor="stripe",
            reason="credit pack",
        )

    def charge(self, milli, *, period=None):
        """Charges are stored NEGATIVE."""
        return AiCreditEntry.objects.create(
            user_id=USER,
            entry_type=AiCreditEntry.ENTRY_CHARGE,
            credits_milli=-milli,
            request_id=uuid.uuid4(),
            period=period or credit_ledger.period_for(),
            plan="pro",
            actor="system",
        )

    def read(self, plan="pro", period=None):
        cache.clear()
        return credit_ledger.balance_breakdown(USER, plan, period=period)


class SpendOrderTests(BucketTestCase):
    """The four states from the product decision, in order."""

    def test_the_allowance_is_spent_before_anything_bought(self):
        self.grant_monthly()
        self.grant_purchased(100_000)
        self.charge(50_000)

        b = self.read()
        self.assertEqual(b.monthly_milli, 20_000, "50 of 70 spent from the allowance")
        self.assertEqual(b.purchased_milli, 100_000, "nothing bought has been touched")
        self.assertEqual(b.total_milli, 120_000)

    def test_only_the_overspill_reaches_the_purchased_bucket(self):
        self.grant_monthly()
        self.grant_purchased(100_000)
        self.charge(90_000)

        b = self.read()
        self.assertEqual(b.monthly_milli, 0, "the allowance is gone")
        self.assertEqual(b.purchased_milli, 80_000, "only the 20 over spilled")
        self.assertEqual(b.total_milli, 80_000)

    def test_a_purchase_after_the_allowance_is_already_gone(self):
        # The likeliest real purchase: you buy BECAUSE you ran out.
        self.grant_monthly()
        self.charge(70_000)
        self.assertEqual(self.read().total_milli, 0)

        self.grant_purchased(100_000)
        b = self.read()
        self.assertEqual(b.monthly_milli, 0)
        self.assertEqual(b.purchased_milli, 100_000, "the whole pack is spendable")

    def test_spending_cannot_drive_a_bucket_negative(self):
        # Overshoot is real: the mid-run stop is cooperative, so a run
        # can end slightly over budget and that excess is ours.
        self.grant_monthly()
        self.grant_purchased(10_000)
        self.charge(200_000)

        b = self.read()
        self.assertEqual(b.monthly_milli, 0)
        self.assertEqual(b.purchased_milli, 0, "drained is empty, not owed")
        self.assertEqual(b.total_milli, 0)


class ExpiryTests(BucketTestCase):
    def test_the_allowance_expires_and_the_purchase_carries(self):
        """The month rollover — the whole reason to buy a pack."""
        last = _last_period()
        self.grant_monthly(period=last)
        self.grant_purchased(100_000, period=last)
        self.charge(50_000, period=last)

        # Reading THIS month: last month's unused 20 is gone, and the
        # lazy grant materialises a fresh allowance.
        b = self.read()
        self.assertEqual(b.monthly_milli, PRO_MILLI, "a new month, a new allowance")
        self.assertEqual(b.purchased_milli, 100_000, "what was bought survived the rollover")
        self.assertEqual(b.total_milli, 170_000)

    def test_a_purchase_survives_even_when_bought_long_ago(self):
        last = _last_period()
        self.grant_purchased(40_000, period=last)
        b = self.read()
        self.assertEqual(b.purchased_milli, 40_000)

    def test_overspend_in_a_past_period_still_drains_the_purchase(self):
        # Otherwise a user could overspend every month and the purchased
        # bucket would refill itself at each rollover.
        last = _last_period()
        self.grant_monthly(period=last)
        self.grant_purchased(100_000, period=last)
        self.charge(100_000, period=last)  # 30 over the allowance

        b = self.read()
        self.assertEqual(b.purchased_milli, 70_000, "last month's 30 overspill stayed spent")


class AllowanceCompositionTests(BucketTestCase):
    def test_an_upgrade_delta_is_one_allowance_not_two(self):
        """A mid-month upgrade posts a DELTA grant (`ensure_monthly_grant`),
        so core→pro is 30_000 + 40_000. Summing those as two separate
        allowances would hand out an extra month."""
        self.grant_monthly(30_000, plan="core")
        self.grant_monthly(40_000, plan="pro")
        self.grant_purchased(10_000)
        self.charge(70_000)

        b = self.read()
        self.assertEqual(b.monthly_milli, 0, "70 granted, 70 spent")
        self.assertEqual(b.purchased_milli, 10_000, "the pack is untouched")

    def test_a_manual_grant_counts_against_the_month_not_the_purchase(self):
        # An operator apology credit is a monthly-side adjustment: it is
        # a make-good for this month, and treating it as purchased would
        # silently make it permanent.
        self.grant_monthly()
        credit_ledger.post_manual(user_id=USER, credits_milli=5_000, reason="apology", actor="op")
        self.grant_purchased(10_000)
        self.charge(75_000)

        b = self.read()
        self.assertEqual(b.monthly_milli, 0, "70 + 5 granted, 75 spent")
        self.assertEqual(b.purchased_milli, 10_000, "still untouched")


class EnforcementCompatibilityTests(BucketTestCase):
    def test_balance_milli_is_the_sum_of_both_buckets(self):
        """Every gate reads this scalar; it must not learn about buckets."""
        self.grant_monthly()
        self.grant_purchased(30_000)
        self.charge(20_000)
        self.assertEqual(credit_ledger.balance_milli(USER, "pro"), 80_000)

    def test_unlimited_still_short_circuits(self):
        self.grant_purchased(100_000)
        self.assertIsNone(credit_ledger.balance_breakdown(USER, "enterprise"))
        self.assertIsNone(credit_ledger.balance_milli(USER, "enterprise"))

    def test_it_fails_open_like_everything_else_on_this_path(self):
        with override_settings(SEARCH_ENGINE=_se()):
            self.assertIsNone(credit_ledger.balance_breakdown("", "pro"))

    def test_a_user_with_no_purchases_reads_exactly_as_before(self):
        self.grant_monthly()
        self.charge(25_000)
        b = self.read()
        self.assertEqual(b.purchased_milli, 0)
        self.assertEqual(b.total_milli, 45_000)


class CacheTests(BucketTestCase):
    def test_a_late_settling_charge_invalidates_the_readable_balance(self):
        """A charge is stamped with the REQUEST's period, not now. One
        settling today from last month changes the purchased bucket, and
        therefore this month's balance — so dropping only last month's
        cache key would leave the live number stale."""
        last = _last_period()
        self.grant_monthly(period=last)
        self.grant_purchased(50_000, period=last)

        credit_ledger.balance_breakdown(USER, "pro")  # warm this month's key

        # Settle a last-month request that overspends last month's
        # allowance by 10, via the real posting path.
        credit_ledger.post_charge(
            user_id=USER,
            request_id=str(uuid.uuid4()),
            credits_milli=80_000,
            period=last,
        )

        b = credit_ledger.balance_breakdown(USER, "pro")  # NOT cleared by hand
        self.assertEqual(b.purchased_milli, 40_000, "the live balance must see the late charge")

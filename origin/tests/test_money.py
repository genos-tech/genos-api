"""Currency handling — USD as the base unit of the cost system.

The generalisation this covers: Genos is going to be sold in more than
one currency, so the money layer had to stop assuming yen. It did NOT
become "multi-currency arithmetic" — it became *single*-currency
arithmetic in the currency providers actually invoice, with conversion
pushed to the edge where a human reads a number.

Three properties, in order of how badly they bite if wrong:

  1. **Conversion never reaches storage.** Every stored figure is
     micro-USD. A converted number in the ledger silently restates
     itself the next time the rate table moves, and then nothing
     reconciles against an invoice.
  2. **Zero-decimal currencies.** ¥1,200 is `1200`, not `120000`.
     Getting this wrong is a factor-of-100 error in what a customer is
     shown, which is why the set lives in one place.
  3. **An unknown currency degrades to USD** rather than raising or,
     far worse, inventing a rate.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine import money

_RATES = {"jpy": 150.0, "eur": 0.92, "gbp": 0.79}


class BaseCurrencyTests(SimpleTestCase):
    def test_the_base_currency_is_usd(self):
        """Providers invoice in USD. Anything else as the base means
        every reconciliation carries an exchange rate."""
        self.assertEqual(money.BASE_CURRENCY, "usd")

    def test_usd_converts_to_itself_untouched(self):
        self.assertEqual(money.convert_usd_micro(1_234_567, "usd", _RATES), 1.234567)

    def test_conversion_uses_the_pinned_rate(self):
        # $2.00 at ¥150/$ = ¥300.
        self.assertAlmostEqual(money.convert_usd_micro(2_000_000, "jpy", _RATES), 300.0)
        self.assertAlmostEqual(money.convert_usd_micro(2_000_000, "eur", _RATES), 1.84)

    def test_an_unknown_currency_falls_back_to_usd(self):
        """A report asked for in a currency we have no pinned rate for
        must print honest dollars. Inventing a rate would put a number
        in front of someone that reconciles against nothing."""
        self.assertEqual(money.convert_usd_micro(1_000_000, "chf", _RATES), 1.0)
        self.assertEqual(money.convert_usd_micro(1_000_000, "", _RATES), 1.0)


class ZeroDecimalTests(SimpleTestCase):
    def test_jpy_has_no_minor_unit(self):
        self.assertEqual(money.minor_units("jpy"), 1)
        self.assertEqual(money.minor_units("JPY"), 1)

    def test_usd_and_eur_have_cents(self):
        self.assertEqual(money.minor_units("usd"), 100)
        self.assertEqual(money.minor_units("eur"), 100)

    def test_the_set_matches_stripe_for_the_ones_we_might_sell_in(self):
        """A currency missing from this set is charged 100x too much in
        Stripe's smallest-unit terms, and shown 100x too small here."""
        for code in ("jpy", "krw", "vnd", "clp"):
            self.assertIn(code, money.ZERO_DECIMAL_CURRENCIES)
        for code in ("usd", "eur", "gbp", "aud", "cad"):
            self.assertNotIn(code, money.ZERO_DECIMAL_CURRENCIES)


class FormattingTests(SimpleTestCase):
    def test_usd_prints_cents(self):
        self.assertEqual(money.format_usd_micro(1_234_500, "usd", _RATES), "$1.23")

    def test_jpy_prints_whole_and_grouped(self):
        # $10 at ¥150 = ¥1,500 — no decimal point, because yen has none.
        self.assertEqual(money.format_usd_micro(10_000_000, "jpy", _RATES), "¥1,500")

    def test_a_symbol_less_currency_prints_its_code(self):
        rates = dict(_RATES, chf=0.88)
        self.assertEqual(money.format_usd_micro(1_000_000, "chf", rates), "0.88 CHF")

    def test_zero_is_zero_in_every_currency(self):
        for code in ("usd", "jpy", "eur"):
            with self.subTest(code=code):
                self.assertIn("0", money.format_usd_micro(0, code, _RATES))


class RateCardTests(SimpleTestCase):
    """`display_fx_per_usd` is deliberately OUTSIDE the fingerprint."""

    def test_display_rates_load_from_the_shipped_catalog(self):
        from django.conf import settings as dj_settings

        card = dj_settings.LLM_CATALOG.rate_card
        self.assertIn("jpy", card.display_fx_per_usd)
        self.assertEqual(card.display_fx_per_usd["jpy"], card.fx_jpy_per_usd)

    def test_adding_a_display_currency_does_not_move_the_fingerprint(self):
        """The rule: the fingerprint covers exactly what can change a
        STORED number. Display rates convert nothing that gets stored,
        so folding them in would split the cost ledger into two regimes
        every time somebody added a currency to read a report in — for
        no change in any number.
        """
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from apis.llm_catalog import load_llm_catalog

        src = Path("apis/llm_models.yaml").read_text(encoding="utf-8")
        with TemporaryDirectory() as d:
            base = Path(d) / "base.yaml"
            base.write_text(src, encoding="utf-8")
            extended = Path(d) / "extended.yaml"
            extended.write_text(
                src.replace("        gbp: 0.79", "        gbp: 0.79\n        chf: 0.88"),
                encoding="utf-8",
            )
            a = load_llm_catalog(base)
            b = load_llm_catalog(extended)

        self.assertIn("chf", b.rate_card.display_fx_per_usd)
        self.assertEqual(
            a.rate_card.fingerprint,
            b.rate_card.fingerprint,
            "adding a display currency must not look like a price change",
        )

    def test_the_stored_jpy_rate_DOES_move_the_fingerprint(self):
        """Its mirror image: `fx_jpy_per_usd` IS written onto rows, so
        changing it genuinely is a new regime."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from apis.llm_catalog import load_llm_catalog

        src = Path("apis/llm_models.yaml").read_text(encoding="utf-8")
        with TemporaryDirectory() as d:
            base = Path(d) / "base.yaml"
            base.write_text(src, encoding="utf-8")
            moved = Path(d) / "moved.yaml"
            moved.write_text(
                src.replace("    fx_jpy_per_usd: 150", "    fx_jpy_per_usd: 160"),
                encoding="utf-8",
            )
            a = load_llm_catalog(base)
            b = load_llm_catalog(moved)

        self.assertNotEqual(a.rate_card.fingerprint, b.rate_card.fingerprint)


class CreditPathHasNoFxTests(SimpleTestCase):
    """The reason any of this happened.

    Credits were usd ->(x150) jpy ->(/15) credits, so the exchange rate
    was baked into every posted charge and what a credit MEANT drifted
    with the yen. For a product about to be sold in several currencies
    that is a defect, not untidiness.
    """

    def test_the_credit_policy_carries_no_currency_rate(self):
        from django.conf import settings as dj_settings

        policy = dj_settings.CREDIT_POLICY
        offenders = [f for f in vars(policy) if "jpy" in f.lower() or "fx" in f.lower()]
        self.assertEqual(
            offenders,
            [],
            "a rate on the credit policy puts FX back into every charge",
        )

    def test_one_credit_is_the_policy_price_in_usd(self):
        from django.conf import settings as dj_settings

        from origin.search_engine import credits

        policy = dj_settings.CREDIT_POLICY
        one_credit_usd_micro = int(round(policy.credit_usd * 1_000_000))
        self.assertEqual(credits.credits_milli(one_credit_usd_micro, policy), 1000)

    def test_the_redenomination_was_exact(self):
        """¥15/credit at the pinned ¥150/$ is exactly $0.10, which is why
        this could be done as a pure re-labelling with no customer's
        balance moving. If either number is edited later this stops being
        true — and that is fine, but it should be a deliberate act."""
        from django.conf import settings as dj_settings

        policy = dj_settings.CREDIT_POLICY
        card = dj_settings.LLM_CATALOG.rate_card
        self.assertAlmostEqual(policy.credit_usd * card.fx_jpy_per_usd, 15.0, places=6)

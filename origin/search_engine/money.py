"""Currency, in one place — pure functions, no Django, no I/O.

**USD is the base unit of the whole cost system**, because that is the
currency every provider actually bills us in. Anthropic, OpenAI, Google,
Tavily and Cohere all invoice in dollars; a yen figure was only ever
something we computed for reading. Making USD the base means the numbers
we reconcile against an invoice need no conversion at all, and the ones
we merely *display* carry their conversion openly.

Everything internal is **micro-USD** (1e-6 USD) — the same unit
`AiSpendEvent.cost_usd_micro` has always used. Integer, never float: this
ledger has to reconcile against a provider invoice months later, and
float sums drift without bound.

## Why credits no longer touch FX

Credits used to be computed `usd -> (x150) -> jpy -> (/15) -> credits`.
Two conversions where zero are needed, and worse: the exchange rate was
baked into every posted charge. What a credit *meant* moved with the yen,
for a product about to be sold in several currencies. Now it is
`usd / credit_usd -> credits`, and the FX table below is display only.

## What this module is NOT for

Customer subscription prices. Those are Stripe's, denominated per
currency by a human who picked a round number that reads well locally —
never converted from a base price at today's rate. `$9` and `¥1,200` are
both deliberate; neither is the other one multiplied by anything. See
`services/stripe_billing.py`.
"""

from __future__ import annotations

BASE_CURRENCY = "usd"

# Currencies with no minor unit — Stripe calls these "zero-decimal", and
# ¥1,200 is stored as `1200`, not `120000`. Getting this wrong is a
# factor-of-100 error in what a customer is shown, so the set lives here
# once rather than in each renderer. Mirrors Stripe's own list.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
)

#: Minor units per major unit, per currency (100 for USD, 1 for JPY).
def minor_units(currency: str) -> int:
    return 1 if (currency or "").lower() in ZERO_DECIMAL_CURRENCIES else 100


def usd_micro_from_usd(usd: float) -> int:
    """Major-unit USD -> integer micro-USD."""
    return int(round(float(usd) * 1_000_000))


def convert_usd_micro(usd_micro: int, currency: str, rates: dict[str, float]) -> float:
    """Micro-USD -> a major-unit amount in `currency`. DISPLAY ONLY.

    Returns a float because it is on its way to a formatter, never to
    storage. Anything that gets stored stays in micro-USD — a converted
    number in the ledger is a number that silently restates itself the
    next time the rate table moves.

    An unknown currency falls back to USD rather than raising: a report
    asked for in a currency we have no pinned rate for should print
    honest dollars, not fail.
    """
    code = (currency or BASE_CURRENCY).lower()
    rate = 1.0 if code == BASE_CURRENCY else float(rates.get(code) or 0.0)
    if rate <= 0:
        rate = 1.0
    return (usd_micro / 1_000_000.0) * rate


#: Symbols for the currencies we actually report in. Absent -> the code
#: is printed instead ("12.34 CHF"), which is ugly but never wrong.
_SYMBOLS = {"usd": "$", "jpy": "¥", "eur": "€", "gbp": "£"}


def format_usd_micro(usd_micro: int, currency: str, rates: dict[str, float]) -> str:
    """Human-readable amount for a CLI report or dashboard.

    Zero-decimal currencies print whole (¥1,234); the rest print two
    decimals ($12.34). Not for customer-facing UI — the frontend formats
    with `Intl.NumberFormat`, which knows far more about placement and
    grouping per locale than a symbol table ever will.
    """
    code = (currency or BASE_CURRENCY).lower()
    amount = convert_usd_micro(usd_micro, code, rates)
    symbol = _SYMBOLS.get(code, "")
    if code in ZERO_DECIMAL_CURRENCIES:
        rendered = f"{symbol}{amount:,.0f}"
    else:
        rendered = f"{symbol}{amount:,.2f}"
    return rendered if symbol else f"{rendered} {code.upper()}"

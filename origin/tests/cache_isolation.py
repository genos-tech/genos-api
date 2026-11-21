"""Every test starts with an empty default cache (MIP-1803).

Why: `apis/settings_test.py` uses `LocMemCache`, which lives as long as the test process and which
Django's `TestCase` never clears. `stripe_billing.price_display` caches a price per price id for an
hour, so whether a test reached the Stripe API (or its mock) depended on whether an earlier test in
the same process had cached that price. Results changed with the order, and with CI's module
sharding (`scripts/ci_test_shard.py`), where a test's neighbours differ from a local whole-suite run.

What it does: `SimpleTestCase._setup_and_call` clears the default cache before each test. A test
that wants a value in the cache sets it itself, in `setUp` or in the test; nothing survives from
the test before it.

Scope: only Django test cases (`SimpleTestCase` and subclasses), like the patch-leak tripwire and
the network guard. The clear runs after `setUpClass`/`setUpTestData`, so a value put in the cache
there is gone by the first test: set it in `setUp`. Only the default alias is cleared (the only one
the test settings define); a test that overrides `CACHES` with its own alias owns that cache.

Only when the default backend is `LocMemCache` (the test settings): a run with other settings leaves a
shared Redis alone. Cost: `LocMemCache.clear()` empties a dict; no database, no network (about 2 us).
"""

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase


def install():
    """Hook every Django test case once. Safe to call again."""
    if getattr(SimpleTestCase, "_cache_isolation", False):
        return
    setup_and_call = SimpleTestCase._setup_and_call

    def _setup_and_call(self, result, debug=False):
        # Only an in-process cache: on any other backend (django-redis under the dev settings) `clear()`
        # flushes a shared server, which a test run must never do.
        if settings.CACHES["default"]["BACKEND"].endswith(".locmem.LocMemCache"):
            cache.clear()
        setup_and_call(self, result, debug)

    SimpleTestCase._setup_and_call = _setup_and_call
    SimpleTestCase._cache_isolation = True

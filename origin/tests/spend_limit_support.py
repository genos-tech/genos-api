"""Put a test month over a monthly AI spend limit (MIP-2363), with a real ledger row."""

import uuid
from contextlib import contextmanager

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings

from origin.search_engine.models import AiSpendEvent


def spend_row(*, team_id="", jpy_milli=0, usd_micro=0, surface="ask"):
    return AiSpendEvent.objects.create(
        request_id=uuid.uuid4(),
        team_id=str(team_id) or None,
        surface=surface,
        cost_jpy_milli=jpy_milli,
        cost_usd_micro=usd_micro,
    )


@contextmanager
def over_the_monthly_budget():
    """The product's budget is 1,000 yen and the month has spent it. Layers over whatever
    `SEARCH_ENGINE` the test already runs with."""
    spend_row(jpy_milli=1_000_000)
    cache.clear()
    try:
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AI_MONTHLY_BUDGET_JPY": 1000.0}
        ):
            yield
    finally:
        cache.clear()

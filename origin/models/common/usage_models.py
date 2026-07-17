"""Per-user usage counters for tier quotas.

`ModelUsageCounter` backs every metered quota dimension configured in
`SEARCH_ENGINE["TIER_QUOTAS"]`. One row per (user, key, UTC day),
where `model_name` is a polymorphic key: a real model id (per-model
daily asks) or a sentinel — `__llm_ask__`, `__web_search__` (daily),
`__task_create__`, `__note_create__` (summed over the calendar month
by `quota.get_used_month`). See `origin.search_engine.quota`.

Ask counters increment at user-initiated agent asks only (not for
internal sub-calls like the query rewriter or reranker), on the FIRST
meaningful event of a run — so disconnects or empty-response failures
don't charge. Creation counters increment after a successful create;
deleting the resource never refunds quota.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class ModelUsageCounter(models.Model):
    """Per-(user, model, UTC-day) ask counter."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="model_usage_counters",
    )
    model_name = models.CharField(max_length=128, db_index=True)
    usage_date = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("user", "model_name", "usage_date")]
        indexes = [models.Index(fields=["user", "usage_date"])]
        verbose_name = "Model Usage Counter"
        verbose_name_plural = "Model Usage Counters"

    def __str__(self) -> str:
        return f"{self.user_id} {self.model_name} {self.usage_date}: {self.count}"

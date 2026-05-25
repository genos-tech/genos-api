"""Per-user usage counters for LLM model selection.

`ModelUsageCounter` enforces daily per-model quotas configured in
`SEARCH_ENGINE["MODEL_DAILY_QUOTAS"]`. One row per (user, model, UTC
day); incremented at user-initiated agent asks only (not for internal
sub-calls like the query rewriter or reranker), so the surfaced
"X / Y used today" matches the user's mental model of "how many asks
I've done."

Counting semantics: incremented on the FIRST `answer_delta` event of
a run — i.e. once the user has received a real response. Disconnects
or empty-response failures before that don't charge.
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

"""Change-feed version store (MIP-970).

One row per "collection of data a client keeps a copy of", named by a key such
as `tasks:p:12` or `notes.personal:t:<team uuid>`. `version` goes up by one each
time something in that collection is written. A client asks
`GET /api/v2/changes/` for the versions it cares about and refetches only the
collections whose token moved, instead of refreshing everything once a minute.

Rows are written ONLY by `origin.services.change_feed` (after the writing
transaction commits, with a single upsert). A key that has never been bumped has
no row and reads as `0`.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class ChangeVersion(models.Model):
    key = models.CharField(max_length=128, unique=True)
    version = models.BigIntegerField(default=0)
    ts_updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.key}={self.version}"

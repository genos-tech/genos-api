"""Ids-only log of erasures, so a backup restore can re-apply them (MIP-2249).

Railway keeps database backups for up to 27 days. A restore brings back every account, team, task
and message that was erased inside that window. This table remembers that an erasure happened:
the kind of object, its id and the time, and NOTHING else (no name, email, title or text), so
keeping it does not keep personal data. `replay_erasures` reads it after a restore and runs the
same deletion services again.

Rows are written by account deletion, the closed-team purge, `purge_deleted_tasks` and message
tombstoning (`origin/services/erasure_log.py`), and pruned after `RETENTION_DAYS` (backup
retention plus a margin) by `prune_housekeeping`.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

#: Railway backups live up to 27 days; 35 leaves a margin for a late restore decision.
RETENTION_DAYS = 35


class ErasureObjectType(models.TextChoices):
    ACCOUNT = "account", "Account"
    TEAM = "team", "Team"
    TASK = "task", "Task"
    MESSAGE = "message", "Message"


class ErasureEvent(models.Model):
    object_type = models.CharField(max_length=16, choices=ErasureObjectType.choices)
    # The object's id as text (a UUID or an integer): never a label.
    object_id = models.CharField(max_length=64)
    erased_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["object_type", "object_id"], name="uniq_erasure_object")
        ]

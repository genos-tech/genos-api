"""What the member told Genos about what matters to them (MIP-1087).

How important an item is to a member is WORKED OUT from their activity (see
`services/importance.py`) and never stored: a stored score goes stale and can be recomputed.
What is stored is only what the member said themselves, two small things per member and team:

  * `MemberImportanceMark`: one row per task the member marked `not_important` (it is never
    offered to them as a top item or as a suggestion), or `priority_is_right` (leave its
    priority alone: no "raise it?" for this item). A task id only, no text, no title.
  * `MemberImportanceTuning`: how much each named signal counts for this member, as
    `{signal: level}`. Only what they changed is stored; an absent signal is `normal`.

A member who never touches either has no row, and everything still works: nothing is ever
asked of them.

**Private to the member it describes.** Every read and write filters on `user`; no teammate,
admin or team dashboard reads it.

**Kept** until the member clears it, turns recording off (`CustomUser.member_events_enabled`:
the PATCH deletes these rows in the same transaction, the same as the ledger and the per-day
table), or deletes the account. No age limit. Exported with the account.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

MARK_NOT_IMPORTANT = "not_important"
MARK_PRIORITY_IS_RIGHT = "priority_is_right"
MARKS = (MARK_NOT_IMPORTANT, MARK_PRIORITY_IS_RIGHT)


class MemberImportanceMark(models.Model):
    mark_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="importance_marks",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # A plain integer, not a foreign key: a mark outlives nothing it should not, and a task
    # the member can no longer open is simply never read back.
    task_id = models.BigIntegerField()
    mark = models.CharField(max_length=24, choices=tuple((m, m) for m in MARKS))
    ts = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "team", "task_id"], name="uniq_importance_mark")
        ]


class MemberImportanceTuning(models.Model):
    tuning_id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        related_name="importance_tunings",
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # `{signal: level}`, validated by `services/importance.py` against its fixed names and levels.
    weights = models.JSONField(default=dict)
    ts = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "team"], name="uniq_importance_tuning")
        ]


def purge_for_user(user_id) -> int:
    """Delete every mark and tuning row of this member. Light on purpose: the opt-out PATCH and
    the account deletion call it without loading the services."""
    deleted, _ = MemberImportanceMark.objects.filter(user_id=user_id).delete()
    MemberImportanceTuning.objects.filter(user_id=user_id).delete()
    return deleted

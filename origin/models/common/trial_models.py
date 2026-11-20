"""The first month of Core, free: one row per person who has had it (MIP-1737).

A trial is NOT a tier write. `CustomUser.tier` and `tier_set_by` belong to Stripe and to the
operator (`stripe_billing._set_personal_tier`, `feature_access set-tier`), and a third writer
would bypass the provenance rules that keep a comp from being erased and a lapsed subscriber from
being kept (SUBSCRIPTION_TIERS.md §8.3). The trial is a separate fact that
`quota.resolve_effective_tier` reads: while `ends_at` is in the future, Core counts as one more
candidate for the best of the person's own tier and their teams' plans. Day 30 is therefore a
clock comparison, not a job: nothing is written, so there is nothing for a Stripe webhook to
overwrite early and nothing to forget to run.

It is its own table, not columns on `CustomUser`, for one reason: `CustomUser.save()` without
`update_fields` writes every column, so a stale in-memory user saved after the trial started would
put NULL back and quietly give the person a second month.

The row is also the "one per person" record. `user` is unique (one per account) and so is
`email_hash`, a copy of the account's own blind index of the lowercased address (the identity the
account system already uses; no new identity data). The copy is what survives account deletion:
deletion anonymises the address, and without it a deleted-and-re-created account would start again.
That holds until the blind-index key is rotated: `rotate_field_keys` cannot re-hash this copy (the plain
address is gone), so rotating the key while the offer runs lets such an account start once more.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from origin.models.common.user_models import CustomUser


class CoreTrial(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="core_trial")
    # `CustomUser.email_hash` at the moment the trial began. NULL only for an account the previous
    # release wrote mid-deploy (NULLs do not collide in a unique column).
    email_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    started_at = models.DateTimeField(default=timezone.now)
    # Fixed when the trial begins, so shortening or lengthening the offer later never moves the
    # end of a trial that is already running.
    ends_at = models.DateTimeField()
    # The two notices before the end (MIP-1764, `services/first_month_trial_notices`). Each is set
    # in the same transaction that files the notice, so a crashed tick redelivers and a second tick
    # never repeats. A stage is also stamped when it was HANDLED without a notice (the plan was
    # already paid, the 1-day notice made the 5-day one stale): the stamp means "this stage is
    # done", not "a person was told", so the tick does not look at the row again. NULL = not yet.
    notified_5d_at = models.DateTimeField(null=True, blank=True, editable=False)
    notified_1d_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["ends_at"], name="coretrial_ends_at"),
            # What the notice tick reads: the rows whose stage is still to do. Partial, so a
            # finished row (the common one) is not in the index at all.
            models.Index(
                fields=["ends_at"],
                name="coretrial_due_5d",
                condition=models.Q(notified_5d_at__isnull=True),
            ),
            models.Index(
                fields=["ends_at"],
                name="coretrial_due_1d",
                condition=models.Q(notified_1d_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"core trial of {self.user_id} until {self.ends_at:%Y-%m-%d}"
